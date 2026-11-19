"""The office decides a driver's expense claim (spec 2026-11-17 §6.4): preview, approve (in full or for less), reject
and undo.

Every test drives the routes the admin Driver expenses page calls, with the bodies it sends:
`POST /api/v1/admin/driver-expenses/<id>/preview` and `/approve` with `{approved_amount}`, `/reject` and `/undo` with
`{reason}`. Trip cash lands through the admin "Record collection" route, claims through the staff bot's Send and
hand-ins through the bot's hand-in tap; the trip is read back through the admin session-detail route and the row
through the page's own detail read. A claim needed in a state no route reaches yet is inserted as setup
(`insert_expense`). Figures are the spec's worked example (the driver collected 420,000 so'm, then claims 150,000 for
fuel). Only Celery is mocked, with a recorder bound against each task's signature.
"""

import inspect
from unittest.mock import Mock

import pytest
from celery.exceptions import Retry
from flask_jwt_extended import create_access_token
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from business_app.models.delivery import DeliveryPerson
from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import DriverCashSession
from business_app.tasks import staff_tasks
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import (
    admin_detail,
    approve,
    claim,
    insert_expense,
    preview_decision,
    reject,
    undo_approval,
)
from tests.integration._prepayment_payout_helpers import (
    SESSION,
    collect_on_trip,
    driver_headers,
    hand_in,
    session_detail,
)
from tests.unit.test_delivery_service_business_rules import _unshadow_task_publish

pytestmark = pytest.mark.integration

PUSH = "staff.push_expense_event"
TELEGRAM_ID = 555
REASON = "Not a business expense"
UNDO_REASON = "Approved by mistake"


# --------------------------------------------------------------------------- fixtures and helpers


@pytest.fixture
def enqueued(monkeypatch):
    """Every Celery publish as `(task name, args, kwargs)`, each bound against the task's own signature."""
    from celery.app.task import Task

    _unshadow_task_publish(monkeypatch)
    calls = []

    def _record(task, args, kwargs):
        inspect.signature(task.run).bind(*args, **kwargs)
        calls.append((task.name, tuple(args), dict(kwargs)))
        return Mock(id="mock-task-id")

    monkeypatch.setattr(Task, "delay", lambda task, *a, **k: _record(task, a, k))
    monkeypatch.setattr(
        Task, "apply_async", lambda task, args=None, kwargs=None, **o: _record(task, args or (), kwargs or {})
    )
    return calls


@pytest.fixture
def audits(monkeypatch):
    """Every audit entry, bound like the real `log_event` call, each marked with whether the claim's last write had
    been committed when it was logged (the payout close test's recorder)."""
    signature = inspect.signature(AuditLogger.log_event)
    calls = []
    seen = {"committed": True}

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        arguments["committed_before_this_call"] = seen["committed"]
        calls.append(arguments)
        return "recorded-audit-event"

    def _after_flush(session, flush_context):
        if any(isinstance(obj, DriverCashExpense) for obj in session.dirty):
            seen["committed"] = False

    def _after_commit(session):
        seen["committed"] = True

    monkeypatch.setattr(audit_logger, "log_event", _record)
    sa_event.listen(Session, "after_flush", _after_flush)
    sa_event.listen(Session, "after_commit", _after_commit)
    yield calls
    sa_event.remove(Session, "after_flush", _after_flush)
    sa_event.remove(Session, "after_commit", _after_commit)


@pytest.fixture
def operator_claim_headers(app, operator_user):
    with app.app_context():
        token = create_access_token(identity=str(operator_user.id), additional_claims={"role": "operator"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.fixture
def driver(db, second_delivery_driver):
    """The spec's driver, with a Telegram chat (`users.telegram_id` is text)."""
    second_delivery_driver.telegram_id = str(TELEGRAM_ID)
    db.session.commit()
    return second_delivery_driver


def _also_drives(db, user):
    """Setup only: an office user who also drives, as the owner's managers and admins sometimes do."""
    user.staff_roles = ["delivery_driver"]
    db.session.add(
        DeliveryPerson(user_id=user.id, full_name=user.full_name, phone=user.phone, is_active=True, is_available=True)
    )
    db.session.commit()


def _trip(client, headers, customer, driver):
    return collect_on_trip(client, headers, customer.id, driver.id, 420000)


def _claim_id(client, app, driver, amount):
    status, answer = claim(client, app, driver, amount)
    assert status == 201, answer
    return answer["data"]["expense"]["id"]


def _pushes(enqueued):
    return [call for call in enqueued if call[0] == PUSH]


def _push(event, expense_id, *, approved_amount, reason, hand_in_now, difference=None):
    payload = {
        "expense_id": expense_id,
        "category": "fuel",
        "amount": 150000.0,
        "approved_amount": approved_amount,
        "difference": difference,
        "reason": reason,
        "hand_in_now": hand_in_now,
    }
    return (PUSH, (TELEGRAM_ID, event, payload), {})


def _trip_figures(client, headers, session_id):
    detail = session_detail(client, headers, session_id)
    return detail["status"], detail["expected_cash_on_hand"], detail["remaining_cash_to_submit"]


def _ok(answer):
    status, body = answer
    assert status == 200, body
    return body["data"]


def _blocked(answer):
    status, body = answer
    return status, body["error_code"], body["details"]["block_code"]


# --------------------------------------------------------------------------- approve


def test_approving_after_a_net_hand_in_closes_the_trip_audits_it_after_commit_and_verifies_clean(
    app, client, db, admin_user, admin_claim_headers, sample_user, driver, enqueued, audits
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    hand_in(client, app, driver, 270000)
    del audits[:]

    status, body = approve(client, admin_claim_headers, expense_id, 150000)

    assert status == 200, body
    data = body["data"]
    assert (data["expense"]["status"], data["expense"]["approved_amount"]) == ("approved", 150000.0)
    assert (data["expense"]["decided_by_user_id"], data["expense"]["self_decided"]) == (admin_user.id, False)
    next_session_id = data["next_session_id"]
    assert next_session_id and next_session_id != sid
    assert _trip_figures(client, admin_claim_headers, sid) == ("submitted", 270000.0, 0.0)
    assert session_detail(client, admin_claim_headers, next_session_id)["status"] == "open"
    assert [c["action"] for c in audits] == ["driver_expense_approved", "driver_cash_session_closed_by_expense"]
    approved, closed = audits
    assert approved["committed_before_this_call"] and closed["committed_before_this_call"]
    assert (approved["resource_type"], approved["resource_id"]) == ("driver_cash_expense", str(expense_id))
    assert approved["additional_data"] == {
        "expense_id": expense_id,
        "driver_user_id": driver.id,
        "driver_cash_session_id": sid,
        "amount": "150000.00",
        "approved_amount": "150000.00",
        "self_decided": False,
        "actor_user_id": admin_user.id,
    }
    assert (closed["event_type"], closed["severity"], closed["resource_id"]) == (
        AuditEventType.PAYMENT_PROCESSED,
        AuditSeverity.MEDIUM,
        str(sid),
    )
    assert closed["additional_data"] == {
        "driver_user_id": driver.id,
        "expense_id": expense_id,
        "actor_user_id": admin_user.id,
        "expected_cash": "420000.00",
        "expected_cash_on_hand": "270000.00",
        "declared_cash": "270000.00",
        "declared_variance": "0.00",
        "status": "submitted",
        "next_driver_cash_session_id": next_session_id,
    }
    verified = client.post(
        SESSION.format(sid) + "/verify",
        json={"verified_cash": 270000, "reason_code": "cash_count_matched"},
        headers=admin_claim_headers,
    )
    assert verified.get_json()["data"]["status"] == "verified"
    assert _pushes(enqueued) == [_push("approved", expense_id, approved_amount=150000.0, reason=None, hand_in_now=0.0)]


def test_approving_less_leaves_the_difference_to_hand_in(
    app, client, db, admin_claim_headers, sample_user, driver, enqueued
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    hand_in(client, app, driver, 270000)

    data = _ok(approve(client, admin_claim_headers, expense_id, 120000))

    assert (data["expense"]["status"], data["expense"]["amount"], data["expense"]["approved_amount"]) == (
        "approved",
        150000.0,
        120000.0,
    )
    assert data["next_session_id"] is None
    assert data["effect"] == {
        "session_id": sid,
        "on_hand_before": 420000.0,
        "on_hand_after": 300000.0,
        "hand_in_now_before": 0.0,
        "hand_in_now_after": 30000.0,
        "will_close": False,
    }
    assert _trip_figures(client, admin_claim_headers, sid) == ("partial", 300000.0, 30000.0)
    assert _pushes(enqueued) == [
        _push("approved_less", expense_id, approved_amount=120000.0, reason=None, hand_in_now=30000.0,
              difference=30000.0)
    ]


def test_approving_a_claim_for_the_whole_trip_cash_closes_the_trip_at_zero(
    app, client, db, admin_claim_headers, sample_user, driver
):
    """Nothing held and nothing handed in, but an approved expense emptied the trip: it closes, as for a return."""
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    status, answer = claim(client, app, driver, 420000)
    assert status == 201, answer
    expense_id = answer["data"]["expense"]["id"]

    preview = _ok(preview_decision(client, admin_claim_headers, expense_id, 420000))
    data = _ok(approve(client, admin_claim_headers, expense_id, 420000))

    assert (
        preview["effect"]
        == data["effect"]
        == {
            "session_id": sid,
            "on_hand_before": 420000.0,
            "on_hand_after": 0.0,
            "hand_in_now_before": 0.0,
            "hand_in_now_after": 0.0,
            "will_close": True,
        }
    )
    assert data["next_session_id"]
    assert _trip_figures(client, admin_claim_headers, sid) == ("submitted", 0.0, 0.0)


def test_approving_more_than_the_claim_is_refused(app, client, db, admin_claim_headers, sample_user, driver, enqueued):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)

    answer = approve(client, admin_claim_headers, expense_id, 150000.01)

    assert _blocked(answer) == (409, "DRIVER_EXPENSE_DECISION_BLOCKED", "amount_above_claim")
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"]["status"] == "pending"
    assert _trip_figures(client, admin_claim_headers, sid) == ("open", 420000.0, 270000.0)
    assert _pushes(enqueued) == []


@pytest.mark.parametrize("action", ["preview", "approve"])
@pytest.mark.parametrize("amount", [0, -1000, "150 000", 1000.001, True, None])
def test_an_amount_that_is_not_money_is_refused(
    app, client, db, admin_claim_headers, sample_user, driver, action, amount
):
    _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    call = preview_decision if action == "preview" else approve

    status, body = call(client, admin_claim_headers, expense_id, amount)

    assert (status, body["error_code"]) == (400, "DRIVER_EXPENSE_AMOUNT_INVALID")
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"]["status"] == "pending"


# --------------------------------------------------------------------------- reject


def test_reject_needs_a_reason_and_gives_the_cash_back_to_hand_in(
    app, client, db, admin_user, admin_claim_headers, sample_user, driver, enqueued, audits
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    hand_in(client, app, driver, 270000)
    n = admin_detail(client, admin_claim_headers, expense_id)["reason_min_length"]

    # Too short once stripped, blank, missing, or not text at all (C13): never a 500.
    for reason in ("", "a" * (n - 1), "   " + "a" * (n - 1) + "\n\t", None, 12345, ["Not a business expense"]):
        status, body = reject(client, admin_claim_headers, expense_id, reason)
        assert (status, body["error_code"]) == (400, "DRIVER_EXPENSE_REASON_REQUIRED"), reason
    status, body = reject(client, admin_claim_headers, expense_id, "a" * (DriverCashExpense.REASON_MAX_LENGTH + 1))
    assert (status, body["error_code"]) == (400, "DRIVER_EXPENSE_REASON_TOO_LONG")
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"]["status"] == "pending"
    del audits[:]

    data = _ok(reject(client, admin_claim_headers, expense_id, f"  {REASON}\n"))

    assert (data["expense"]["status"], data["expense"]["decision_reason"], data["hand_in_now"]) == (
        "rejected",
        REASON,
        150000.0,
    )
    assert (data["expense"]["approved_amount"], data["expense"]["decided_by_user_id"]) == (None, admin_user.id)
    assert _trip_figures(client, admin_claim_headers, sid) == ("partial", 420000.0, 150000.0)
    assert [(c["action"], c["committed_before_this_call"], c["additional_data"]) for c in audits] == [
        (
            "driver_expense_rejected",
            True,
            {
                "expense_id": expense_id,
                "driver_user_id": driver.id,
                "driver_cash_session_id": sid,
                "amount": "150000.00",
                "approved_amount": None,
                "self_decided": False,
                "actor_user_id": admin_user.id,
                "reason": REASON,
            },
        )
    ]
    # A second decision is refused.
    assert _blocked(approve(client, admin_claim_headers, expense_id, 150000)) == (
        409,
        "DRIVER_EXPENSE_DECISION_BLOCKED",
        "not_pending",
    )
    assert _pushes(enqueued) == [
        _push("rejected", expense_id, approved_amount=None, reason=REASON, hand_in_now=150000.0)
    ]


# --------------------------------------------------------------------------- self-decision


def test_a_manager_cannot_decide_his_own_claim_and_an_admin_is_flagged(
    app,
    client,
    db,
    admin_user,
    admin_claim_headers,
    manager_user,
    manager_claim_headers,
    sample_user,
):
    for user in (manager_user, admin_user):
        _also_drives(db, user)
        _trip(client, admin_claim_headers, sample_user, user)
    his = _claim_id(client, app, manager_user, 150000)

    for answer in (
        approve(client, manager_claim_headers, his, 150000),
        reject(client, manager_claim_headers, his, REASON),
    ):
        status, body = answer
        assert (status, body["error_code"], body["details"]) == (
            403,
            "DRIVER_EXPENSE_SELF_DECISION",
            {"expense_id": his},
        )
    assert admin_detail(client, admin_claim_headers, his)["expense"]["status"] == "pending"

    by_admin = _ok(approve(client, admin_claim_headers, his, 150000))["expense"]
    assert (by_admin["status"], by_admin["self_decided"], by_admin["decided_by_user_id"]) == (
        "approved",
        False,
        admin_user.id,
    )
    status, body = undo_approval(client, manager_claim_headers, his, UNDO_REASON)
    assert (status, body["error_code"]) == (403, "DRIVER_EXPENSE_SELF_DECISION")
    assert admin_detail(client, admin_claim_headers, his)["expense"]["status"] == "approved"

    own = _claim_id(client, app, admin_user, 150000)
    by_himself = _ok(approve(client, admin_claim_headers, own, 150000))["expense"]
    assert (by_himself["status"], by_himself["self_decided"], by_himself["decided_by_user_id"]) == (
        "approved",
        True,
        admin_user.id,
    )
    db.session.expire_all()
    assert db.session.get(DriverCashExpense, own).self_decided is True


# --------------------------------------------------------------------------- undo


def test_undo_of_a_full_approval_while_the_trip_is_active_changes_nothing_to_hand_in(
    app, client, db, admin_user, admin_claim_headers, sample_user, driver, enqueued, audits
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    _ok(approve(client, admin_claim_headers, expense_id, 150000))
    assert _trip_figures(client, admin_claim_headers, sid) == ("open", 270000.0, 270000.0)
    del audits[:]

    data = _ok(undo_approval(client, admin_claim_headers, expense_id, UNDO_REASON))

    expense = data["expense"]
    assert (expense["status"], expense["approved_amount"], expense["decided_at"]) == ("pending", None, None)
    assert (expense["decided_by_user_id"], expense["decision_reason"], expense["self_decided"]) == (None, None, False)
    assert data["hand_in_now"] == 270000.0
    assert _trip_figures(client, admin_claim_headers, sid) == ("open", 420000.0, 270000.0)
    assert [(c["action"], c["committed_before_this_call"], c["additional_data"]) for c in audits] == [
        (
            "driver_expense_approval_undone",
            True,
            {
                "expense_id": expense_id,
                "driver_user_id": driver.id,
                "driver_cash_session_id": sid,
                "amount": "150000.00",
                "approved_amount": None,
                "self_decided": False,
                "actor_user_id": admin_user.id,
                "reason": UNDO_REASON,
                "undone_approved_amount": "150000.00",
            },
        )
    ]
    assert _pushes(enqueued) == [
        _push("approved", expense_id, approved_amount=150000.0, reason=None, hand_in_now=270000.0),
        _push("approval_undone", expense_id, approved_amount=None, reason=UNDO_REASON, hand_in_now=270000.0),
    ]
    # Waiting again: the office decides it again.
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"]["can_approve"] is True


def test_undo_after_the_difference_was_handed_in_is_refused(
    app, client, db, admin_claim_headers, sample_user, driver, enqueued
):
    """Spec 6.4: undoing A would put its full 150,000 back in waiting and take "Hand in now" to −50,000."""
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    a = _claim_id(client, app, driver, 150000)
    _claim_id(client, app, driver, 50000)
    _ok(approve(client, admin_claim_headers, a, 100000))
    hand_in(client, app, driver, 270000)

    answer = undo_approval(client, admin_claim_headers, a, UNDO_REASON)

    assert _blocked(answer) == (409, "DRIVER_EXPENSE_DECISION_BLOCKED", "cash_already_handed_in")
    row = admin_detail(client, admin_claim_headers, a)["expense"]
    assert (row["undo_block_code"], row["can_undo"]) == ("cash_already_handed_in", False)
    assert (row["status"], row["approved_amount"]) == ("approved", 100000.0)
    assert _trip_figures(client, admin_claim_headers, sid) == ("partial", 320000.0, 0.0)
    assert [call[1][1] for call in _pushes(enqueued)] == ["approved_less"]


def test_undo_after_the_approval_closed_the_trip_is_refused(
    app, client, db, admin_claim_headers, sample_user, driver, enqueued
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    hand_in(client, app, driver, 270000)
    _ok(approve(client, admin_claim_headers, expense_id, 150000))

    answer = undo_approval(client, admin_claim_headers, expense_id, UNDO_REASON)

    assert _blocked(answer) == (409, "DRIVER_EXPENSE_DECISION_BLOCKED", "session_checked")
    row = admin_detail(client, admin_claim_headers, expense_id)["expense"]
    assert (row["undo_block_code"], row["can_undo"], row["status"]) == ("session_checked", False, "approved")
    assert _trip_figures(client, admin_claim_headers, sid) == ("submitted", 270000.0, 0.0)
    assert [call[1][1] for call in _pushes(enqueued)] == ["approved"]


# --------------------------------------------------------------------------- two claims, and the preview


def test_two_waiting_claims_cap_each_other_and_close_the_trip_only_when_both_are_decided(
    app, client, db, admin_claim_headers, sample_user, driver
):
    """Review Focus 1: each approval subtracts only its own claim; the other waiting claim keeps the trip open."""
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    a = _claim_id(client, app, driver, 150000)
    b = _claim_id(client, app, driver, 50000)
    hand_in(client, app, driver, 220000)  # his "Hand in now": 420,000 − 200,000 waiting

    first = _ok(approve(client, admin_claim_headers, a, 150000))

    assert (first["effect"]["will_close"], first["next_session_id"]) == (False, None)
    assert _trip_figures(client, admin_claim_headers, sid) == ("partial", 270000.0, 0.0)
    assert _ok(preview_decision(client, admin_claim_headers, b, 50000))["approve_max_amount"] == 50000.0

    second = _ok(approve(client, admin_claim_headers, b, 50000))

    assert second["effect"]["will_close"] is True and second["next_session_id"]
    assert _trip_figures(client, admin_claim_headers, sid) == ("submitted", 220000.0, 0.0)
    assert session_detail(client, admin_claim_headers, second["next_session_id"])["status"] == "open"


def test_the_preview_is_what_approve_does(app, client, db, admin_claim_headers, sample_user, driver, enqueued):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    hand_in(client, app, driver, 270000)

    preview = _ok(preview_decision(client, admin_claim_headers, expense_id, 150000))

    assert preview == {
        "effect": {
            "session_id": sid,
            "on_hand_before": 420000.0,
            "on_hand_after": 270000.0,
            "hand_in_now_before": 0.0,
            "hand_in_now_after": 0.0,
            "will_close": True,
        },
        "blocking_codes": [],
        "approve_max_amount": 150000.0,
    }
    # The preview wrote nothing and told no one.
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"]["status"] == "pending"
    assert _trip_figures(client, admin_claim_headers, sid) == ("partial", 420000.0, 0.0)
    assert _pushes(enqueued) == []

    assert _ok(approve(client, admin_claim_headers, expense_id, 150000))["effect"] == preview["effect"]


def test_a_driver_without_telegram_gets_no_push(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, enqueued
):
    driver = second_delivery_driver
    assert driver.telegram_id is None
    _trip(client, admin_claim_headers, sample_user, driver)
    approved_id = _claim_id(client, app, driver, 100000)
    rejected_id = _claim_id(client, app, driver, 50000)

    _ok(approve(client, admin_claim_headers, approved_id, 100000))
    _ok(reject(client, admin_claim_headers, rejected_id, REASON))
    _ok(undo_approval(client, admin_claim_headers, approved_id, UNDO_REASON))

    assert _pushes(enqueued) == []


def test_a_push_the_broker_refuses_leaves_the_decision_recorded(
    app, client, db, admin_claim_headers, sample_user, driver, monkeypatch
):
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)
    refused = []

    def refuse(*args, **kwargs):
        inspect.signature(staff_tasks.push_expense_event.run).bind(*args, **kwargs)
        refused.append(args)
        raise ConnectionError("broker down")

    # On the task's own __dict__, which monkeypatch restores to absent (test_delivery_service_business_rules).
    monkeypatch.setitem(vars(staff_tasks.push_expense_event), "delay", refuse)

    data = _ok(approve(client, admin_claim_headers, expense_id, 150000))

    assert (data["expense"]["status"], [args[:2] for args in refused]) == ("approved", [(TELEGRAM_ID, "approved")])
    assert _trip_figures(client, admin_claim_headers, sid) == ("open", 270000.0, 270000.0)


# --------------------------------------------------------------------------- one rule for the row, preview and route


@pytest.mark.parametrize("action", ["approve", "reject"])
@pytest.mark.parametrize("world", ["waiting", "approved", "trip_closed", "own_claim"])
def test_the_row_flag_the_preview_codes_and_the_routes_refusal_agree(
    app,
    client,
    db,
    admin_user,
    admin_claim_headers,
    manager_user,
    manager_claim_headers,
    sample_user,
    second_delivery_driver,
    world,
    action,
):
    """One rule (`rules.decide_blocks`): the row's flag, the preview's first code and the write's refusal agree."""
    owner, headers = second_delivery_driver, admin_claim_headers
    if world == "own_claim":
        _also_drives(db, manager_user)
        owner, headers = manager_user, manager_claim_headers
    sid = _trip(client, admin_claim_headers, sample_user, owner)
    if world == "approved":
        expense_id = insert_expense(db, sid, owner.id, 150000, status="approved", decided_by=admin_user.id)
    else:
        expense_id = _claim_id(client, app, owner, 150000)
    if world == "trip_closed":
        # Defensive: no route closes a trip while a claim waits on it.
        db.session.get(DriverCashSession, sid).status = DriverCashSessionStatus.VERIFIED
        db.session.commit()

    row = admin_detail(client, headers, expense_id)["expense"]
    codes = _ok(preview_decision(client, headers, expense_id, 1000))["blocking_codes"]
    if action == "approve":
        status, body = approve(client, headers, expense_id, 1000)
    else:
        status, body = reject(client, headers, expense_id, REASON)

    code = row["decide_block_code"]
    assert code == {"waiting": None, "approved": "not_pending", "trip_closed": "trip_not_active"}.get(
        world, "self_decision"
    )
    assert codes[:1] == ([code] if code else [])
    assert row["can_reject"] is (code is None) and (status == 200) is (code is None), body
    if code == "self_decision":
        assert (status, body["error_code"]) == (403, "DRIVER_EXPENSE_SELF_DECISION")
    elif code:
        assert (status, body["error_code"], body["details"]["block_code"]) == (
            409,
            "DRIVER_EXPENSE_DECISION_BLOCKED",
            code,
        )


# --------------------------------------------------------------------------- who decides


_CALLS = {
    "preview": lambda http, headers, expense_id: preview_decision(http, headers, expense_id, 150000),
    "approve": lambda http, headers, expense_id: approve(http, headers, expense_id, 150000),
    "reject": lambda http, headers, expense_id: reject(http, headers, expense_id, REASON),
    "undo": lambda http, headers, expense_id: undo_approval(http, headers, expense_id, UNDO_REASON),
}


@pytest.mark.parametrize("who", ["operator", "driver"])
@pytest.mark.parametrize("action", ["preview", "approve", "reject", "undo"])
def test_only_admins_and_managers_decide(
    request, app, client, db, admin_user, admin_claim_headers, sample_user, driver, enqueued, who, action
):
    """Spec 6.12: 403, nothing written, no push."""
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    if action == "undo":
        expense_id = insert_expense(db, sid, driver.id, 150000, status="approved", decided_by=admin_user.id)
    else:
        expense_id = _claim_id(client, app, driver, 150000)
    before = admin_detail(client, admin_claim_headers, expense_id)["expense"]
    headers = driver_headers(app, driver) if who == "driver" else request.getfixturevalue("operator_claim_headers")

    status, body = _CALLS[action](app.test_client(), headers, expense_id)

    assert status == 403 and "data" not in body, body
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"] == before
    assert _pushes(enqueued) == []


@pytest.mark.parametrize("action", ["preview", "approve", "reject", "undo"])
def test_an_unknown_claim_is_not_found(app, db, admin_claim_headers, action):
    status, body = _CALLS[action](app.test_client(), admin_claim_headers, 999999)

    assert (status, body["error_code"]) == (404, "DRIVER_EXPENSE_NOT_FOUND")


@pytest.mark.parametrize(
    "action,code",
    [
        ("preview", "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ("approve", "DRIVER_EXPENSE_AMOUNT_INVALID"),
        ("reject", "DRIVER_EXPENSE_REASON_REQUIRED"),
        ("undo", "DRIVER_EXPENSE_REASON_REQUIRED"),
    ],
)
def test_a_body_that_is_not_an_object_is_refused_with_the_fields_code(
    app, client, db, admin_claim_headers, sample_user, driver, action, code
):
    _trip(client, admin_claim_headers, sample_user, driver)
    expense_id = _claim_id(client, app, driver, 150000)

    response = app.test_client().post(
        f"/api/v1/admin/driver-expenses/{expense_id}/{action}", json=[150000, REASON], headers=admin_claim_headers
    )

    assert (response.status_code, response.get_json()["error_code"]) == (400, code)


# --------------------------------------------------------------------------- the push task


@pytest.fixture
def webhooks(monkeypatch):
    """Every staff-bot webhook the task sends, bound against `_send_staff_webhook`; each answers `result`."""
    signature = inspect.signature(staff_tasks._send_staff_webhook)
    sent, outcome = [], {"result": True}

    def _send(*args, **kwargs):
        sent.append(dict(signature.bind(*args, **kwargs).arguments))
        return outcome["result"]

    monkeypatch.setattr(staff_tasks, "_send_staff_webhook", _send)
    return sent, outcome


def test_the_push_task_posts_to_the_expense_route_keyed_on_its_own_id(webhooks):
    sent, _ = webhooks
    payload = {"expense_id": 7, "category": "fuel", "amount": 150000.0}

    staff_tasks.push_expense_event.push_request(id="task-abc")
    try:
        staff_tasks.push_expense_event.run(TELEGRAM_ID, "approved", payload)
    finally:
        staff_tasks.push_expense_event.pop_request()

    assert sent == [
        {
            "endpoint": "/internal/expense-event",
            "data": {
                "telegram_id": TELEGRAM_ID,
                "event": "approved",
                "payload": payload,
                "event_id": "expense-event:task-abc",
            },
        }
    ]


def test_the_push_task_retries_a_failed_webhook(webhooks, monkeypatch):
    _, outcome = webhooks
    outcome["result"] = False
    retries = []

    def fake_retry(*args, **kwargs):
        inspect.signature(staff_tasks.push_expense_event.retry).bind(*args, **kwargs)
        retries.append(kwargs)
        raise Retry()

    monkeypatch.setattr(staff_tasks.push_expense_event, "retry", fake_retry)
    with pytest.raises(Retry):
        staff_tasks.push_expense_event.run(TELEGRAM_ID, "rejected", {"expense_id": 7})
    assert len(retries) == 1 and "rejected" in str(retries[0]["exc"])
    assert (staff_tasks.push_expense_event.max_retries, staff_tasks.push_expense_event.default_retry_delay) == (3, 60)
