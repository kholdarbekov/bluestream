"""REAL POSTGRES: a driver's cash expense claim racing the other writers of his trip (spec 2026-11-17 §6.6).

The trip's session row (FOR NO KEY UPDATE) is the LAST lock of every expense write. A claim takes only that row
(`find_active_session(for_update=True)`), because it inserts; withdraw and the office's decisions lock the claim's row
first and the session row after it (`lock_session`). A force-close and a collected-cash correction take the session
row and never an expense row. So:

* A claim and a force-close. The claim first: the force-close counts it under the session lock and refuses
  (CASH_SESSION_EXPENSES_PENDING). The force-close first: the claim's locking read re-checks the row it waited on,
  finds the trip closed, and refuses (DRIVER_EXPENSE_NO_ACTIVE_TRIP).
* A claim and a collected-cash correction, on a partial trip (the event adjust refuses an open trip before it takes
  any lock). The claim first: the correction's unlocked pre-check saw no claim, so it waits on the session lock and is
  refused there, rolling back whole; the reversal's own-session audit rows outlive the rollback and one more says it
  was undone. Sent again, it is refused by the pre-check before it writes anything. The correction first: the claim's
  cap reads the corrected figures.
* Withdraw and approve on one claim: the claim's row is the first lock of both, so the second waits there and reads
  what the first decided.
* Two identical Sends at once (Review Focus 2): the second waits on the session row, finds the key under the lock and
  answers the first's claim again.

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`).
The interleaving is forced as in test_prepayment_payout_races_pg_e2e.py: the first side is held right after it took
the session row, the second starts only then and is proved to be waiting on a lock before the first is released.
Every wait is bounded. Every answer is asserted exactly, so none is a 500 and no deadlock (40P01) aborted either side.
After each race the trip is read the way the cash report reads it, its list row (the grouped SUM(CASE) read of the
waiting and approved totals) beside its detail. Celery publishing is the only thing faked (`enqueued`).

Run: bash scripts/precommit-backend-tests.sh tests/integration/test_driver_expense_races_pg_e2e.py -x
"""

import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import func

from business_app.models.audit import AuditLog
from business_app.models.delivery import DeliveryPerson
from business_app.models.driver_expense import DriverCashExpense
from business_app.models.payment import CashCollectionEvent
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from shared.enums import UserRole, UserType
from tests.integration._driver_expense_helpers import ADMIN, EXPENSES, WITHDRAW, claim, door_cash_on_trip, options
from tests.integration._prepayment_payout_helpers import BASE, collect_on_trip, hand_in, session_detail
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread, _user
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.unit._scope_money_helpers import delivered_cod_order, make_address

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

FORCE_CLOSE = BASE + "/sessions/{}/force-close"
ADJUST = BASE + "/events/{}/adjust"
APPROVE = ADMIN + "/{}/approve"
CLAIMED = "staff.alert_driver_expense_claimed"
FLOOR_CODE = "EVENT_FUNDS_DRIVER_EXPENSE"
# What the cash report publishes for a trip, in its list row and its detail alike.
FIGURES = (
    "status",
    "expected_cash_on_hand",
    "expenses_waiting_total",
    "expenses_approved_total",
    "remaining_cash_to_submit",
    "force_close_block_code",
)

# Where the side that goes first is held: right after it took the trip's session row, its last lock. A claim takes it
# through `find_active_session(for_update=True)`; every other writer here through `lock_session` (withdraw and approve
# after the claim's row, a correction after the collection's).
CLAIM_HOLDS = (DriverReconciliationService, "find_active_session", lambda kwargs: bool(kwargs.get("for_update")))
OTHERS_HOLD = (DriverReconciliationService, "lock_session", lambda kwargs: True)


def _world(pg_db, pg_app):
    """A customer, an admin and a driver with his DeliveryPerson row, with the headers their screens send."""
    customer = _user(pg_db, "+998901281001", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    admin = _user(pg_db, "+998901281002", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver = _user(pg_db, "+998901281003", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    pg_db.session.add(DeliveryPerson(user_id=driver.id, full_name="Race Driver", phone=driver.phone, is_active=True))
    pg_db.session.commit()
    return SimpleNamespace(
        customer=customer,
        driver=driver,
        client=pg_app.test_client(),
        admin_headers={
            "Authorization": "Bearer "
            + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
            "Content-Type": "application/json",
        },
        driver_headers={
            "Authorization": "Bearer " + create_access_token(identity=str(driver.id)),
            "Content-Type": "application/json",
        },
    )


def _cash_on_trip(world, amount):
    """The admin "Record collection" modal: the customer paid `amount` ahead to the driver, so it is in his trip
    cash. Returns the trip's id."""
    return collect_on_trip(world.client, world.admin_headers, world.customer.id, world.driver.id, amount)


def _send(name, pg_app, world, answers, errors, amount, key, finally_set=None):
    """The staff bot's Send on a fuel claim of `amount` under the draft's `key`, with the body it posts, on its own
    thread."""
    body = {"category": "fuel", "amount": amount, "note": None, "receipt_file_id": None, "idempotency_key": key}
    return _thread(
        name,
        pg_app,
        lambda client: client.post(EXPENSES, json=body, headers=world.driver_headers),
        answers,
        errors,
        finally_set=finally_set,
    )


def _admin_post(name, pg_app, world, url, body, answers, errors, finally_set=None):
    """An admin screen's POST of `body` to `url`, on its own thread."""
    return _thread(
        name,
        pg_app,
        lambda client: client.post(url, json=body, headers=world.admin_headers),
        answers,
        errors,
        finally_set=finally_set,
    )


def _race(pg_db, monkeypatch, *, holder, hold_at, first, second):
    """Start `first(holds)` (a thread named `holder`), hold it right after `hold_at` (owner, method, which call of it
    takes the lock) has returned to it, start `second()`, prove it waits on a lock, then release both."""
    holds, release = threading.Event(), threading.Event()
    owner, name, takes_the_lock = hold_at
    original = getattr(owner, name)

    def held(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if threading.current_thread().name == holder and takes_the_lock(kwargs) and not holds.is_set():
            holds.set()
            assert release.wait(timeout=30), "the test never released the side holding the lock"
        return result

    monkeypatch.setattr(owner, name, held)
    first_thread, second_thread = first(holds), second()
    first_thread.start()
    try:
        assert holds.wait(timeout=30), "the first side never reached its hold point"
        second_thread.start()
        assert _await_a_lock_waiter(pg_db), f"the second side did not wait: nothing holds the lock {name} takes"
        assert second_thread.is_alive(), f"the second side finished while the first held the lock {name} takes"
    finally:
        release.set()
        _finish(first_thread, *([second_thread] if second_thread.ident else []))


def _outcome(answer):
    """(status, error code, block code) of an answer. The staff and Driver expenses routes publish the code at the top
    level; the cash reconciliation routes under `data`."""
    status, body = answer
    code = body.get("error_code") or (body.get("data") or {}).get("error_code")
    return status, code, (body.get("details") or {}).get("block_code")


def _trip_figures(client, headers, session_id):
    """The trip as the cash report shows it. Its row in the session list (every row's claim totals in ONE grouped
    SUM(CASE) read) must publish what its detail does; returns those figures."""
    listed = client.get(BASE, headers=headers)
    assert listed.status_code == 200, listed.get_json()
    (row,) = [row for row in listed.get_json()["data"]["sessions"] if row["id"] == session_id]
    detail = session_detail(client, headers, session_id)
    figures = {key: detail[key] for key in FIGURES}
    assert {key: row[key] for key in FIGURES} == figures
    return figures


def _claims(pg_db):
    """Every claim row, oldest first: (id, amount, approved amount, status)."""
    pg_db.session.expire_all()
    return [
        (row.id, float(row.amount), float(row.approved_amount) if row.approved_amount is not None else None, row.status)
        for row in DriverCashExpense.query.order_by(DriverCashExpense.id)
    ]


def _order_events(pg_db, order_id):
    """The order's collections, oldest first: (id, amount, voided)."""
    pg_db.session.expire_all()
    rows = CashCollectionEvent.query.filter_by(order_id=order_id).order_by(CashCollectionEvent.id)
    return [(row.id, float(row.amount), row.voided_at is not None) for row in rows]


def _last_audit_id(pg_db):
    return pg_db.session.query(func.coalesce(func.max(AuditLog.id), 0)).scalar()


def _collection_audits(pg_db, *, after):
    """The audit rows on collections written after audit row `after`, oldest first."""
    pg_db.session.expire_all()
    return (
        AuditLog.query.filter(AuditLog.id > after, AuditLog.resource_type == "cash_collection_event")
        .order_by(AuditLog.id)
        .all()
    )


# --------------------------------------------------------------------------- a claim and a force-close


@pytest.mark.parametrize("first", ["claim", "force_close"], ids=["claim-first", "force-close-first"])
def test_a_claim_and_a_force_close_never_both_succeed(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    world = _world(pg_db, pg_app)
    session_id = _cash_on_trip(world, 100000)
    answers, errors = {}, []
    sides = {
        "claim": lambda holds=None: _send("claim", pg_app, world, answers, errors, 30000, "k-fuel", holds),
        "force_close": lambda holds=None: _admin_post(
            "force_close",
            pg_app,
            world,
            FORCE_CLOSE.format(session_id),
            {"reason": "Driver left the company", "verified_cash": 100000},
            answers,
            errors,
            holds,
        ),
    }
    second = "force_close" if first == "claim" else "claim"

    _race(
        pg_db,
        monkeypatch,
        holder=first,
        hold_at=CLAIM_HOLDS if first == "claim" else OTHERS_HOLD,
        first=sides[first],
        second=sides[second],
    )

    assert errors == []
    if first == "claim":
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "claim": (201, None, None),
            "force_close": (409, "CASH_SESSION_EXPENSES_PENDING", None),
        }
        assert _claims(pg_db) == [(answers["claim"][1]["data"]["expense"]["id"], 30000.0, None, "pending")]
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "open",
            "expected_cash_on_hand": 100000.0,
            "expenses_waiting_total": 30000.0,
            "expenses_approved_total": 0.0,
            "remaining_cash_to_submit": 70000.0,
            "force_close_block_code": "expenses_pending",
        }
    else:
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "force_close": (200, None, None),
            "claim": (409, "DRIVER_EXPENSE_NO_ACTIVE_TRIP", None),
        }
        assert _claims(pg_db) == []
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "force_closed",
            "expected_cash_on_hand": 100000.0,
            "expenses_waiting_total": 0.0,
            "expenses_approved_total": 0.0,
            "remaining_cash_to_submit": 100000.0,
            "force_close_block_code": "session_status",
        }
        # The force-close opens no next trip.
        assert options(world.client, pg_app, world.driver)["has_active_trip"] is False


# --------------------------------------------------------------------------- a claim and a collected-cash correction


@pytest.mark.parametrize("first", ["claim", "correction"], ids=["claim-first", "correction-first"])
def test_a_claim_and_a_collected_cash_correction(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    """100 000 collected at the door, 70 000 of it handed in: the trip is partial and "Hand in now" is 30 000. The
    driver claims 30 000 for fuel while the admin corrects the collection to 90 000."""
    world = _world(pg_db, pg_app)
    order, _payment = delivered_cod_order(
        pg_db, world.customer, address=make_address(pg_db, world.customer), total=Decimal("100000.00")
    )
    order_id = order.id
    event_id = door_cash_on_trip(pg_db, order, world.customer, world.driver, total=100000, cash=100000)
    # Partial, as the event adjust refuses an open trip before it takes any lock.
    trip = hand_in(world.client, pg_app, world.driver, 70000)
    assert trip["status"] == "partial"
    session_id = trip["id"]
    audited = _last_audit_id(pg_db)
    correction = {"new_amount": 90000, "reason": "Driver miscounted"}
    answers, errors = {}, []
    sides = {
        "claim": lambda holds=None: _send("claim", pg_app, world, answers, errors, 30000, "k-fuel", holds),
        "correction": lambda holds=None: _admin_post(
            "correction", pg_app, world, ADJUST.format(event_id), correction, answers, errors, holds
        ),
    }
    second = "correction" if first == "claim" else "claim"

    _race(
        pg_db,
        monkeypatch,
        holder=first,
        hold_at=CLAIM_HOLDS if first == "claim" else OTHERS_HOLD,
        first=sides[first],
        second=sides[second],
    )

    assert errors == []
    if first == "claim":
        # The correction's unlocked pre-check ran before the claim committed; under the session lock, after its
        # replacement's refresh, it finds 90 000 below the 70 000 handed in plus the 30 000 waiting.
        why = (
            f"Collection {event_id}: this correction would leave trip #{session_id} 90000.00 on hand, below what the "
            "driver handed in plus the expenses waiting (100000.00); decide the waiting expense first"
        )
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "claim": (201, None, None),
            "correction": (400, FLOOR_CODE, None),
        }
        assert answers["correction"][1]["errors"] == [why]
        # The correction rolled back whole: the collection stands and the claim waits.
        assert _order_events(pg_db, order_id) == [(event_id, 100000.0, False)]
        assert _claims(pg_db) == [(answers["claim"][1]["data"]["expense"]["id"], 30000.0, None, "pending")]
        # The reversal's and the replacement's audit rows were committed through the audit logger's own session and
        # outlive the rollback; the entry after them says they were undone.
        trail = _collection_audits(pg_db, after=audited)
        replacement_id = int(trail[1].resource_id)
        assert [(row.action, row.resource_id, row.success) for row in trail] == [
            ("cash_collection_reversed", str(event_id), True),
            ("cash_collection_posted", str(replacement_id), True),
            ("cash_collection_reversal_refused", str(event_id), False),
        ]
        assert (trail[2].error_message, trail[2].additional_data) == (
            "EVENT_FUNDS_DRIVER_EXPENSE: the reversal would take back cash an approved expense paid out",
            {"driver_cash_session_id": session_id, "error_code": FLOOR_CODE, "replacement_event_id": replacement_id},
        )
        assert pg_db.session.get(CashCollectionEvent, replacement_id) is None

        # Sent again, the same correction is refused by the unlocked pre-check, before it writes anything.
        retry = world.client.post(ADJUST.format(event_id), json=correction, headers=world.admin_headers)

        assert (retry.status_code, retry.get_json()["data"]["error_code"], retry.get_json()["errors"]) == (
            400,
            FLOOR_CODE,
            [why],
        )
        assert len(_collection_audits(pg_db, after=audited)) == 3
        assert _order_events(pg_db, order_id) == [(event_id, 100000.0, False)]
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "partial",
            "expected_cash_on_hand": 100000.0,
            "expenses_waiting_total": 30000.0,
            "expenses_approved_total": 0.0,
            "remaining_cash_to_submit": 0.0,
            "force_close_block_code": "expenses_pending",
        }
    else:
        # The claim's cap read the corrected trip: 90 000 on hand, 70 000 handed in, so 20 000 to spend, and the
        # 30 000 that fitted before is refused.
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "correction": (200, None, None),
            "claim": (409, "DRIVER_EXPENSE_ABOVE_CASH_HELD", None),
        }
        assert answers["claim"][1]["details"] == {"max_amount": 20000.0}
        replacement_id = answers["correction"][1]["data"]["cash_collection_event"]["id"]
        assert _claims(pg_db) == []
        assert _order_events(pg_db, order_id) == [(event_id, 100000.0, True), (replacement_id, 90000.0, False)]
        assert [(row.action, row.resource_id, row.success) for row in _collection_audits(pg_db, after=audited)] == [
            ("cash_collection_reversed", str(event_id), True),
            ("cash_collection_posted", str(replacement_id), True),
            ("cash_collection_amount_adjusted", str(event_id), True),
        ]
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "partial",
            "expected_cash_on_hand": 90000.0,
            "expenses_waiting_total": 0.0,
            "expenses_approved_total": 0.0,
            "remaining_cash_to_submit": 20000.0,
            "force_close_block_code": None,
        }


# --------------------------------------------------------------------------- withdraw and approve on one claim


@pytest.mark.parametrize("first", ["withdraw", "approve"], ids=["withdraw-first", "approve-first"])
def test_withdraw_and_approve_on_one_claim(pg_app, pg_db, monkeypatch, enqueued, first):  # noqa: F811
    """100 000 on the trip, a 30 000 claim raced and a 20 000 claim waiting throughout, so the trip's totals sum a
    waiting claim beside an approved one."""
    world = _world(pg_db, pg_app)
    session_id = _cash_on_trip(world, 100000)
    status, body = claim(world.client, pg_app, world.driver, 30000)
    assert status == 201, body
    expense_id = body["data"]["expense"]["id"]
    status, body = claim(world.client, pg_app, world.driver, 20000)
    assert status == 201, body
    other_id = body["data"]["expense"]["id"]
    answers, errors = {}, []
    sides = {
        "withdraw": lambda holds=None: _thread(
            "withdraw",
            pg_app,
            lambda client: client.post(WITHDRAW.format(expense_id), headers=world.driver_headers),
            answers,
            errors,
            finally_set=holds,
        ),
        "approve": lambda holds=None: _admin_post(
            "approve", pg_app, world, APPROVE.format(expense_id), {"approved_amount": 30000}, answers, errors, holds
        ),
    }
    second = "approve" if first == "withdraw" else "withdraw"

    _race(pg_db, monkeypatch, holder=first, hold_at=OTHERS_HOLD, first=sides[first], second=sides[second])

    assert errors == []
    if first == "withdraw":
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "withdraw": (200, None, None),
            "approve": (409, "DRIVER_EXPENSE_DECISION_BLOCKED", "not_pending"),
        }
        assert _claims(pg_db) == [(expense_id, 30000.0, None, "withdrawn"), (other_id, 20000.0, None, "pending")]
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "open",
            "expected_cash_on_hand": 100000.0,
            "expenses_waiting_total": 20000.0,
            "expenses_approved_total": 0.0,
            "remaining_cash_to_submit": 80000.0,
            "force_close_block_code": "expenses_pending",
        }
    else:
        assert {name: _outcome(answer) for name, answer in answers.items()} == {
            "approve": (200, None, None),
            "withdraw": (409, "DRIVER_EXPENSE_NOT_WITHDRAWABLE", None),
        }
        assert _claims(pg_db) == [(expense_id, 30000.0, 30000.0, "approved"), (other_id, 20000.0, None, "pending")]
        assert _trip_figures(world.client, world.admin_headers, session_id) == {
            "status": "open",
            "expected_cash_on_hand": 70000.0,
            "expenses_waiting_total": 20000.0,
            "expenses_approved_total": 30000.0,
            "remaining_cash_to_submit": 50000.0,
            "force_close_block_code": "expenses_pending",
        }


# --------------------------------------------------------------------------- two identical Sends at once


def test_two_identical_sends_at_once_record_one_claim(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    """Review Focus 2: both Sends miss the key before the lock; the second waits on the session row behind the first,
    and once it holds the row it finds the key the first committed and answers that claim again."""
    world = _world(pg_db, pg_app)
    session_id = _cash_on_trip(world, 100000)
    enqueued.clear()
    answers, errors = {}, []

    _race(
        pg_db,
        monkeypatch,
        holder="first_send",
        hold_at=CLAIM_HOLDS,
        first=lambda holds: _send("first_send", pg_app, world, answers, errors, 30000, "k-double", holds),
        second=lambda: _send("second_send", pg_app, world, answers, errors, 30000, "k-double"),
    )

    assert errors == []
    first_status, first = answers["first_send"]
    second_status, second = answers["second_send"]
    assert (first_status, first["data"]["replayed"]) == (201, False), first
    assert (second_status, second["data"]["replayed"]) == (200, True), second
    # Both answers carry the one claim and the trip's figures after it.
    assert second["data"] == {**first["data"], "replayed": True}
    assert (first["data"]["hand_in_now"], first["data"]["expenses_waiting_total"]) == (70000.0, 30000.0)
    expense_id = first["data"]["expense"]["id"]
    assert _claims(pg_db) == [(expense_id, 30000.0, None, "pending")]
    assert enqueued == [(CLAIMED, (expense_id,), {})]
    assert _trip_figures(world.client, world.admin_headers, session_id) == {
        "status": "open",
        "expected_cash_on_hand": 100000.0,
        "expenses_waiting_total": 30000.0,
        "expenses_approved_total": 0.0,
        "remaining_cash_to_submit": 70000.0,
        "force_close_block_code": "expenses_pending",
    }
