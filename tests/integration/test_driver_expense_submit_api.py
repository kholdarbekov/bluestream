"""A driver records a cash expense claim from the staff bot (spec 2026-11-17 §6.3).

Every test drives the two routes the staff bot calls, with the body it sends: `GET /api/v1/staff/expenses/options`
before the flow starts, and `POST /api/v1/staff/expenses` on Send. Trip cash lands through the admin "Record
collection" route with the driver as collector, and the trip is read back through the admin session-detail route.
Expected figures are the spec's worked example (the driver collected 420,000 so'm, then claims 150,000 for fuel).
"""

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from business_app.models.delivery import DeliveryPerson
from business_app.models.driver_expense import DriverCashExpense
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.tasks import staff_tasks
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from tests.integration._driver_expense_helpers import EXPENSE_OPTIONS, EXPENSES, claim, options
from tests.integration._prepayment_payout_helpers import collect_on_trip, session_detail

pytestmark = pytest.mark.integration

ROW_KEYS = {
    "id",
    "category",
    "amount",
    "approved_amount",
    "note",
    "has_receipt",
    "status",
    "submitted_at",
    "decided_at",
    "decision_reason",
    "driver_cash_session_id",
}


def _row(db, expense_id):
    db.session.expire_all()
    return db.session.get(DriverCashExpense, expense_id)


# --------------------------------------------------------------------------- the claim and the trip's cash


def test_a_claim_lowers_hand_in_now_and_leaves_on_hand(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    assert options(client, app, driver) == {
        "has_active_trip": True,
        "session_id": sid,
        "max_amount": 420000.0,
        "expenses_waiting_total": 0.0,
    }

    status, body = claim(client, app, driver, 150000, key="k1", receipt_file_id="AgACAgIAAxkBAAIB")

    assert status == 201, body
    data = body["data"]
    expense = data["expense"]
    assert set(expense) == ROW_KEYS
    assert (expense["status"], expense["amount"], expense["category"]) == ("pending", 150000.0, "fuel")
    assert (expense["approved_amount"], expense["note"], expense["has_receipt"]) == (None, None, True)
    assert (expense["decided_at"], expense["decision_reason"], expense["driver_cash_session_id"]) == (None, None, sid)
    assert (data["hand_in_now"], data["expenses_waiting_total"], data["replayed"]) == (270000.0, 150000.0, False)
    row = _row(db, expense["id"])
    assert (row.driver_user_id, row.idempotency_key, row.receipt_file_id) == (driver.id, "k1", "AgACAgIAAxkBAAIB")
    d = session_detail(client, admin_claim_headers, sid)
    assert (d["expected_cash_on_hand"], d["remaining_cash_to_submit"]) == (420000.0, 270000.0)
    assert options(client, app, driver)["expenses_waiting_total"] == 150000.0


def test_an_amount_above_hand_in_now_is_refused(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)

    status, body = claim(client, app, driver, 420000.01)

    assert (status, body["error_code"], body["details"]["max_amount"]) == (
        409,
        "DRIVER_EXPENSE_ABOVE_CASH_HELD",
        420000.0,
    )
    assert DriverCashExpense.query.count() == 0


def test_a_second_claim_is_capped_by_the_first(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    """The cap is "Hand in now", which the first claim already lowered."""
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    assert claim(client, app, driver, 150000)[0] == 201

    status, body = claim(client, app, driver, 270000.01)

    assert (status, body["error_code"], body["details"]["max_amount"]) == (
        409,
        "DRIVER_EXPENSE_ABOVE_CASH_HELD",
        270000.0,
    )
    assert claim(client, app, driver, 270000)[0] == 201
    assert options(client, app, driver)["max_amount"] == 0.0


def test_options_read_the_stored_figures_without_a_lock(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    driver = second_delivery_driver
    spies = SimpleNamespace(find_active_session=[], lock_session=[], refresh_expected_cash=[])

    def spy(name):
        original = getattr(DriverReconciliationService, name)
        calls = getattr(spies, name)

        def recording(self, *args, **kwargs):
            calls.append(SimpleNamespace(args=args, kwargs=kwargs))
            return original(self, *args, **kwargs)

        monkeypatch.setattr(DriverReconciliationService, name, recording)

    for name in ("find_active_session", "lock_session", "refresh_expected_cash"):
        spy(name)
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    assert claim(client, app, driver, 150000)[0] == 201
    for calls in vars(spies).values():
        calls.clear()

    assert options(client, app, driver)["max_amount"] == 270000.0  # the stored figures are current

    assert [c.kwargs.get("for_update", False) for c in spies.find_active_session] == [False]
    assert spies.lock_session == [] and spies.refresh_expected_cash == []


def test_without_an_active_trip_the_claim_is_refused_and_options_say_so(app, client, db, second_delivery_driver):
    driver = second_delivery_driver

    assert options(client, app, driver) == {
        "has_active_trip": False,
        "session_id": None,
        "max_amount": 0.0,
        "expenses_waiting_total": 0.0,
    }
    status, body = claim(client, app, driver, 1000)
    assert (status, body["error_code"]) == (409, "DRIVER_EXPENSE_NO_ACTIVE_TRIP")
    assert DriverCashExpense.query.count() == 0


# --------------------------------------------------------------------------- resends


def test_a_resend_after_the_claim_used_all_the_cash_returns_the_same_claim(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)

    first = claim(client, app, driver, 420000, key="k1")
    # The note is not compared, as the payout ignores its reason: same category and amount is the same claim.
    again = claim(client, app, driver, 420000, key="k1", note="Resent from the confirmation screen")

    assert (first[0], again[0]) == (201, 200), again
    replayed = again[1]["data"]
    assert replayed["expense"] == first[1]["data"]["expense"]
    assert (replayed["replayed"], replayed["hand_in_now"], replayed["expenses_waiting_total"]) == (True, 0.0, 420000.0)
    assert DriverCashExpense.query.count() == 1


@pytest.mark.parametrize("change", [{"amount": 160000}, {"category": "caps"}])
def test_a_key_reused_for_a_different_claim_is_refused(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, change
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    assert claim(client, app, driver, 150000, key="k1")[0] == 201

    body = {"category": "fuel", "amount": 150000, **change}
    status, answer = claim(client, app, driver, body.pop("amount"), key="k1", **body)

    assert (status, answer["error_code"]) == (409, "DRIVER_EXPENSE_IDEMPOTENCY_KEY_REUSED")
    assert DriverCashExpense.query.count() == 1


def test_another_drivers_key_is_his_own(
    app, client, db, admin_claim_headers, sample_user, second_sample_user, delivery_driver, second_delivery_driver
):
    """The key is unique per driver: the same key from two drivers records two claims."""
    collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    collect_on_trip(client, admin_claim_headers, second_sample_user.id, delivery_driver.id, 100000)

    assert claim(client, app, second_delivery_driver, 150000, key="k1")[0] == 201
    assert claim(client, app, delivery_driver, 50000, key="k1")[0] == 201
    assert DriverCashExpense.query.count() == 2


# --------------------------------------------------------------------------- the body


@pytest.mark.parametrize(
    "body,code",
    [
        ({"category": "bottles"}, "DRIVER_EXPENSE_CATEGORY_INVALID"),
        ({"category": None}, "DRIVER_EXPENSE_CATEGORY_INVALID"),
        ({"category": "other", "note": None}, "DRIVER_EXPENSE_NOTE_REQUIRED"),
        # Postgres trim() strips only spaces, so "empty" is decided here.
        ({"category": "other", "note": " \n\t "}, "DRIVER_EXPENSE_NOTE_REQUIRED"),
        ({"note": 5}, "DRIVER_EXPENSE_NOTE_REQUIRED"),
        ({"note": "x" * 501}, "DRIVER_EXPENSE_NOTE_TOO_LONG"),
        ({"amount": "150 000"}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": True}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": 0}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": -5}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": 150000.001}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": "1e5"}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"amount": None}, "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ({"idempotency_key": ""}, "DRIVER_EXPENSE_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": None}, "DRIVER_EXPENSE_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": "k" * 65}, "DRIVER_EXPENSE_IDEMPOTENCY_KEY_REQUIRED"),
        ({"receipt_file_id": 5}, "DRIVER_EXPENSE_RECEIPT_INVALID"),
        ({"receipt_file_id": ""}, "DRIVER_EXPENSE_RECEIPT_INVALID"),
        ({"receipt_file_id": "f" * 256}, "DRIVER_EXPENSE_RECEIPT_INVALID"),
    ],
)
def test_the_body_is_validated(app, client, db, admin_claim_headers, sample_user, second_delivery_driver, body, code):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    payload = {"category": "fuel", "amount": 150000, **body}

    status, answer = claim(client, app, driver, payload.pop("amount"), **payload)

    assert (status, answer["error_code"]) == (400, code), answer
    assert DriverCashExpense.query.count() == 0


def test_an_other_claim_keeps_its_stripped_note(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)

    status, body = claim(client, app, driver, 30000.5, category="other", note="  Tyre puncture repair \n")

    assert status == 201, body
    expense = body["data"]["expense"]
    assert (expense["category"], expense["note"], expense["amount"], expense["has_receipt"]) == (
        "other",
        "Tyre puncture repair",
        30000.5,
        False,
    )


def test_the_location_is_copied_when_fresh_and_empty_when_stale(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    person = DeliveryPerson.query.filter_by(user_id=driver.id).one()

    def claim_with_fix(minutes_old):
        person.current_location_lat, person.current_location_lng = 41.3111, 69.2797
        person.last_location_update = datetime.now(UTC) - timedelta(minutes=minutes_old)
        db.session.commit()
        status, body = claim(client, app, driver, 1000)
        assert status == 201, body
        row = _row(db, body["data"]["expense"]["id"])
        return row.latitude, row.longitude

    assert claim_with_fix(10) == (41.3111, 69.2797)
    assert claim_with_fix(31) == (None, None)


def test_a_new_claim_is_audited_after_commit_and_a_replay_is_not(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    signature = inspect.signature(AuditLogger.log_event)
    calls = []
    seen = {"committed": False}

    def record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        arguments["committed_before_this_call"] = seen["committed"]
        calls.append(arguments)
        return "recorded-audit-event"

    def after_flush(session, flush_context):
        if any(isinstance(obj, DriverCashExpense) for obj in session.new):
            seen["committed"] = False

    def after_commit(session):
        seen["committed"] = True

    monkeypatch.setattr(audit_logger, "log_event", record)
    sa_event.listen(Session, "after_flush", after_flush)
    sa_event.listen(Session, "after_commit", after_commit)
    try:
        status, body = claim(client, app, driver, 150000, key="k1")
        replay_status, _ = claim(client, app, driver, 150000, key="k1")
    finally:
        sa_event.remove(Session, "after_flush", after_flush)
        sa_event.remove(Session, "after_commit", after_commit)

    assert (status, replay_status) == (201, 200), body
    expense_id = body["data"]["expense"]["id"]
    assert calls == [
        {
            "event_type": AuditEventType.PAYMENT_PROCESSED,
            "action": "driver_expense_submitted",
            "severity": AuditSeverity.MEDIUM,
            "resource_type": "driver_cash_expense",
            "resource_id": str(expense_id),
            "description": None,
            "old_values": None,
            "new_values": None,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "expense_id": expense_id,
                "driver_user_id": driver.id,
                "driver_cash_session_id": sid,
                "category": "fuel",
                "amount": "150000.00",
            },
            "committed_before_this_call": True,
        }
    ]


# --------------------------------------------------------------------------- who may call


def test_an_operator_cannot_record_a_claim(client, db, operator_auth_headers):
    for response in (
        client.get(EXPENSE_OPTIONS, headers=operator_auth_headers),
        client.post(
            EXPENSES, json={"category": "fuel", "amount": 1000, "idempotency_key": "k1"}, headers=operator_auth_headers
        ),
    ):
        assert (response.status_code, response.get_json()["error_code"]) == (403, "STAFF_NO_ROLE")
    assert DriverCashExpense.query.count() == 0


def test_a_slack_alert_the_broker_refuses_leaves_the_claim_recorded(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    refused = []

    def refuse(*args, **kwargs):
        inspect.signature(staff_tasks.alert_driver_expense_claimed.run).bind(*args, **kwargs)
        refused.append(args)
        raise ConnectionError("broker down")

    # On the task's own __dict__, which monkeypatch restores to absent (test_delivery_service_business_rules).
    monkeypatch.setitem(vars(staff_tasks.alert_driver_expense_claimed), "delay", refuse)

    status, body = claim(client, app, driver, 150000)

    assert status == 201, body
    expense = _row(db, body["data"]["expense"]["id"])
    assert (expense.status, expense.amount, [args for args in refused]) == (
        DriverCashExpense.PENDING,
        150000,
        [(expense.id,)],
    )
    assert session_detail(client, admin_claim_headers, sid)["expenses_waiting_total"] == 150000.0
