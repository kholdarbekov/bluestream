"""REAL POSTGRES: a prepayment return racing a delivery that spends the same credit.

The return draws the customer's free credit under lock 1, the same id-ordered, re-qualifying lock
the delivery's credit application takes (`CashCollectionService._lock_credit_events_by_ids`):

* The delivery first. It holds the credit events while it settles a 30 000 debt. The return waits
  on the lock, then plans against the rows the delivery committed: 20 000 left, so a 50 000 return
  is refused (amount_exceeds_free_balance) and a 20 000 one goes through. Nothing is drawn twice.
* The return first. It holds the events while it pays 50 000 out. The delivery waits, then its
  locking read re-qualifies the rows (`unapplied_amount > 0`) and finds none: it applies nothing,
  and the debt stays open.
* Set balance (P12) behind a delivery. The amount is derived from the rows the return locked, not
  from a figure read before it waited, so "leave 0" pays out what the delivery left and never more.

"From his current trip's cash" adds lock 3, the driver's cash session row (FOR NO KEY UPDATE), which the
driver's handoff and the admin's force-close take too (see the section at the end of this file).

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres
database (`pg_app`). The interleaving is forced the way test_bottle_transfer_races_pg_e2e.py forces
its own: the first side is held right after `_lock_credit_events_by_ids` returns, the second starts
only then and is proved to be waiting on a lock. Every wait is bounded. Celery publishing is the only
thing faked (`enqueued`).

Run: docker run --rm --network bluestream_default --env-file .env -v "$(pwd):/app" -w /app -e FLASK_ENV=testing
-e TESTING=true bluestream-business_app sh -c 'pip install -q pytest-xdist >/dev/null 2>&1;
pytest tests/integration/test_prepayment_payout_races_pg_e2e.py -n0 --no-cov --no-header -q'
"""

import inspect
import os
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token
from sqlalchemy import func, text
from sqlalchemy.exc import OperationalError

from business_app.models.corporate import CorporatePrepaymentAccount, CorporatePrepaymentLedger
from business_app.models.delivery import DeliveryPerson
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    CustomerCreditPayout,
    DriverCashHandoff,
    DriverCashSession,
    Payment,
)
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.corporate_contract_service import CorporateContractService
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from business_app.services.notification_service import NotificationService
from business_app.tasks import payment_tasks
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from shared.enums import DriverCashSessionStatus, OrderStatus, UserRole, UserType
from tests.integration.test_customer_cancel_races_pg import _await_a_lock_waiter, _finish, _thread, _user
from tests.integration.test_redispatch_reschedules_to_today import enqueued  # noqa: F401 -- a fixture
from tests.unit._scope_money_helpers import delivered_cod_order, make_address
from tests.unit.test_order_cash_edit_service import _make_grocery_amount_contract

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

APPLY = "/api/v1/admin/staff/cash-reconciliation/customers/{}/prepayment-payouts"


def _world(pg_db, *, debt):
    """A customer holding 50 000 of free credit and, booked after it, a delivered COD debt of `debt`."""
    customer = _user(pg_db, "+998901261001", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    admin = _user(pg_db, "+998901261002", role=UserRole.ADMIN, user_type=UserType.STAFF)
    CashCollectionService().post_collection(
        customer_id=customer.id,
        amount=Decimal("50000.00"),
        source="admin_adjustment",
        recorded_by_user_id=admin.id,
        notes="Prepaid in cash at the office",
    )
    pg_db.session.commit()
    _, payment = delivered_cod_order(pg_db, customer, address=make_address(pg_db, customer), total=Decimal(debt))
    headers = {
        "Authorization": "Bearer " + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }
    return customer.id, payment.id, headers


def _return_body(key, **amount):
    return {"paid_by": "office", "reason": "Customer moved away", "idempotency_key": key, **amount}


def _delivery_applies_credit(pg_app, payment_id, answers, errors, finally_set=None):
    """The delivery's credit application (`apply_customer_prepaid_credit_to_payment`), committed, on its own
    thread, app context, session and connection."""

    def run():
        with pg_app.app_context():
            from business_app import db

            try:
                payment = db.session.get(Payment, payment_id)
                CashCollectionService().apply_customer_prepaid_credit_to_payment(payment)
                db.session.commit()
                answers["delivery"] = "committed"
            except Exception as exc:  # noqa: BLE001 -- named by the assertion that reads `errors`
                db.session.rollback()
                errors.append(f"delivery: {exc!r}")
            finally:
                if finally_set is not None:
                    finally_set.set()

    return threading.Thread(target=run, name="delivery")


def _race(
    pg_app,
    pg_db,
    monkeypatch,
    *,
    holder,
    first,
    second,
    lock=(CashCollectionService, "_lock_credit_events_by_ids"),
    takes_the_lock=lambda kwargs: True,
):
    """Start `first` (a thread named `holder`), hold it right after `lock` (owner, method name) has returned
    to it, start `second`, prove it waits on that lock, then release. `takes_the_lock(kwargs)` says which call
    of the method is the locking one (the credit-event lock always is; the session lookup only `for_update`)."""
    holds = threading.Event()
    release = threading.Event()
    owner, name = lock
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


def _ledger(pg_db, customer_id, payment_id):
    pg_db.session.expire_all()
    unapplied = (
        pg_db.session.query(func.coalesce(func.sum(CashCollectionEvent.unapplied_amount), 0))
        .filter(CashCollectionEvent.customer_id == customer_id)
        .scalar()
    )
    payment = pg_db.session.get(Payment, payment_id)
    return Decimal(str(unapplied)), Decimal(str(payment.outstanding_amount))


def test_a_delivery_spending_the_credit_and_a_return_draw_it_once(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    customer_id, payment_id, headers = _world(pg_db, debt="30000.00")
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="delivery",
        first=lambda holds: _delivery_applies_credit(pg_app, payment_id, answers, errors, finally_set=holds),
        second=lambda: _thread(
            "return",
            pg_app,
            lambda client: client.post(
                APPLY.format(customer_id), json=_return_body("k-1", amount=50000), headers=headers
            ),
            answers,
            errors,
        ),
    )

    assert errors == []
    assert answers["delivery"] == "committed"
    status, body = answers["return"]
    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert body["errors"][0].startswith("amount_exceeds_free_balance:")
    assert _ledger(pg_db, customer_id, payment_id) == (Decimal("20000.00"), Decimal("0.00"))

    response = pg_app.test_client().post(
        APPLY.format(customer_id), json=_return_body("k-2", amount=20000), headers=headers
    )

    assert response.status_code == 201, response.get_json()
    assert _ledger(pg_db, customer_id, payment_id) == (Decimal("0.00"), Decimal("0.00"))
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


def test_a_return_holding_the_credit_leaves_the_delivery_nothing(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    customer_id, payment_id, headers = _world(pg_db, debt="30000.00")
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        first=lambda holds: _thread(
            "return",
            pg_app,
            lambda client: client.post(
                APPLY.format(customer_id), json=_return_body("k-1", amount=50000), headers=headers
            ),
            answers,
            errors,
            finally_set=holds,
        ),
        second=lambda: _delivery_applies_credit(pg_app, payment_id, answers, errors),
    )

    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    assert body["data"]["payout"]["amount"] == 50000.0
    assert answers["delivery"] == "committed"
    assert _ledger(pg_db, customer_id, payment_id) == (Decimal("0.00"), Decimal("30000.00"))
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


def test_set_balance_under_a_concurrent_spend_never_overshoots(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    customer_id, payment_id, headers = _world(pg_db, debt="20000.00")
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="delivery",
        first=lambda holds: _delivery_applies_credit(pg_app, payment_id, answers, errors, finally_set=holds),
        second=lambda: _thread(
            "return",
            pg_app,
            lambda client: client.post(
                APPLY.format(customer_id), json=_return_body("k-1", target_balance=0), headers=headers
            ),
            answers,
            errors,
        ),
    )

    assert errors == []
    assert answers["delivery"] == "committed"
    status, body = answers["return"]
    assert status == 201, body
    assert body["data"]["payout"]["amount"] == 30000.0
    assert body["data"]["available_prepayment_balance"] == 0.0
    pg_db.session.expire_all()
    assert Decimal(str(CustomerCreditPayout.query.one().amount)) == Decimal("30000.00")
    assert _ledger(pg_db, customer_id, payment_id) == (Decimal("0.00"), Decimal("0.00"))
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


# --------------------------------------------------------------------------- "from his current trip's cash"
#
# Lock 3 is the driver's cash session row (`find_active_session(..., for_update=True)`, FOR NO KEY UPDATE).
# The driver's handoff takes the same lock (`get_or_create_session(for_update=True)`), and so does every admin
# writer that can act on an active session (`lock_session`: verify, resolve, force-close). Review Focus 2:
# neither side may over-draw, on_hand never drops below what was handed in, and no session is left PARTIAL
# with nothing owed.

SESSION_LOCK = (DriverReconciliationService, "find_active_session")
HAND_IN = "/api/v1/staff/reconciliation/session/submit"
FORCE_CLOSE = "/api/v1/admin/staff/cash-reconciliation/sessions/{}/force-close"


def _for_update(kwargs):
    return bool(kwargs.get("for_update"))


def _trip_world(pg_db):
    """A driver carrying 100 000 that a customer paid him ahead on his current trip: owed nothing, it is the
    customer's free credit and it is in the driver's cash session."""
    customer = _user(pg_db, "+998901261101", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    admin = _user(pg_db, "+998901261102", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver = _user(pg_db, "+998901261103", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    pg_db.session.add(DeliveryPerson(user_id=driver.id, full_name="Race Driver", phone=driver.phone, is_active=True))
    pg_db.session.commit()
    event = CashCollectionService().post_collection(
        customer_id=customer.id,
        amount=Decimal("100000.00"),
        source="standalone_meeting",
        collector_user_id=driver.id,
        recorded_by_user_id=admin.id,
        notes="Paid ahead to the driver",
    )
    pg_db.session.commit()
    return SimpleNamespace(
        customer_id=customer.id,
        driver_id=driver.id,
        session_id=event.driver_cash_session_id,
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


def _trip_return(pg_app, world, answers, errors, amount, finally_set=None):
    """The admin records a return of `amount` from the driver's current trip cash, on its own thread."""
    return _thread(
        "return",
        pg_app,
        lambda client: client.post(
            APPLY.format(world.customer_id),
            json={
                "amount": amount,
                "paid_by": "driver_current_session",
                "driver_user_id": world.driver_id,
                "reason": "Customer moved away",
                "idempotency_key": f"trip-{amount}",
            },
            headers=world.admin_headers,
        ),
        answers,
        errors,
        finally_set=finally_set,
    )


def _hand_in(pg_app, world, answers, errors, amount, finally_set=None, name="handoff"):
    """The driver's "hand in" tap for `amount` (None: the "settle everything" figure), on its own thread."""
    return _thread(
        name,
        pg_app,
        lambda client: client.post(HAND_IN, json={"declared_cash": amount}, headers=world.driver_headers),
        answers,
        errors,
        finally_set=finally_set,
    )


def _session(pg_db, session_id):
    pg_db.session.expire_all()
    return pg_db.session.get(DriverCashSession, session_id)


def _active_sessions(pg_db, driver_id):
    """The driver's not-yet-closed sessions: one at most (uq_driver_cash_sessions_driver_active)."""
    pg_db.session.expire_all()
    return DriverCashSession.query.filter(
        DriverCashSession.driver_user_id == driver_id,
        DriverCashSession.status.in_(
            [DriverCashSessionStatus(status) for status in DriverReconciliationService.ACTIVE_STATUSES]
        ),
    ).all()


def test_a_return_behind_a_handoff_is_capped_by_it(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="handoff",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _hand_in(pg_app, world, answers, errors, 60000, finally_set=holds),
        second=lambda: _trip_return(pg_app, world, answers, errors, 50000),
    )

    assert errors == []
    status, body = answers["handoff"]
    assert (status, body["data"]["status"]) == (200, "partial"), body
    status, body = answers["return"]
    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert body["errors"][0].startswith("amount_exceeds_driver_cash:")
    session = _session(pg_db, world.session_id)
    assert (
        session.status,
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.declared_cash)),
    ) == (DriverCashSessionStatus.PARTIAL, Decimal("100000.00"), Decimal("60000.00"))
    assert CustomerCreditPayout.query.count() == 0


def test_a_handoff_behind_a_return_sees_the_lower_on_hand(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _trip_return(pg_app, world, answers, errors, 50000, finally_set=holds),
        second=lambda: _hand_in(pg_app, world, answers, errors, 60000),
    )

    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    assert body["data"]["driver_cash_session"]["expected_cash_on_hand"] == 50000.0
    status, body = answers["handoff"]
    assert status == 200, body
    # The handoff planned against the 50 000 the driver holds after the return, not the 100 000 he collected:
    # 60 000 handed in closes the trip with a 10 000 surplus; it never sits PARTIAL with nothing owed.
    assert (body["data"]["status"], body["data"]["declared_variance"]) == ("submitted", 10000.0)
    assert body["data"]["next_active_session"] is not None
    session = _session(pg_db, world.session_id)
    assert (
        session.status,
        Decimal(str(session.expected_cash)),
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.declared_cash)),
        Decimal(str(session.declared_variance)),
    ) == (
        DriverCashSessionStatus.SUBMITTED,
        Decimal("100000.00"),
        Decimal("50000.00"),
        Decimal("60000.00"),
        Decimal("10000.00"),
    )


def test_a_force_close_behind_a_return_writes_the_lower_on_hand(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _trip_return(pg_app, world, answers, errors, 40000, finally_set=holds),
        second=lambda: _thread(
            "force_close",
            pg_app,
            lambda client: client.post(
                FORCE_CLOSE.format(world.session_id),
                json={"reason": "Driver left the company", "verified_cash": 55000},
                headers=world.admin_headers,
            ),
            answers,
            errors,
        ),
    )

    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    status, body = answers["force_close"]
    assert status == 200, body
    # Counted 55 000 against the 60 000 the driver holds after the return: 5 000 short, not 45 000.
    assert (
        body["data"]["status"],
        body["data"]["expected_cash_on_hand"],
        body["data"]["verified_cash"],
        body["data"]["verified_variance"],
    ) == ("force_closed", 60000.0, 55000.0, -5000.0)
    session = _session(pg_db, world.session_id)
    assert (
        session.status,
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.verified_variance)),
    ) == (DriverCashSessionStatus.FORCE_CLOSED, Decimal("60000.00"), Decimal("-5000.00"))


# A writer that CLOSES the trip while another waits on its row. FOR NO KEY UPDATE re-checks the row it waited
# on: the closed session no longer matches "active", so the waiting statement finds none, and its snapshot
# predates the next session the closer opened. The handoff must not open a second active session from that
# stale read (a unique-index 500), nor record the driver's figure on the brand-new empty trip: the trip he was
# settling changed under him, so he is told and nothing is written.


@pytest.mark.parametrize("declared_cash", [60000, None], ids=["typed-amount", "settle-everything"])
def test_a_handoff_behind_a_closing_return_is_refused(
    pg_app, pg_db, monkeypatch, enqueued, declared_cash  # noqa: F811
):
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _trip_return(pg_app, world, answers, errors, 100000, finally_set=holds),
        second=lambda: _hand_in(pg_app, world, answers, errors, declared_cash),
    )

    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    assert body["data"]["driver_cash_session"]["status"] == "submitted"
    next_session_id = body["data"]["next_session_id"]
    status, body = answers["handoff"]
    assert status == 409, body
    assert (body["error"], body["error_code"]) == ("CONFLICT", "RECONCILIATION_SESSION_CHANGED")
    session = _session(pg_db, world.session_id)
    assert (session.status, session.declared_cash, Decimal(str(session.expected_cash_on_hand))) == (
        DriverCashSessionStatus.SUBMITTED,
        None,
        Decimal("0.00"),
    )
    assert [active.id for active in _active_sessions(pg_db, world.driver_id)] == [next_session_id]
    assert DriverCashHandoff.query.count() == 0


def test_a_second_tap_behind_a_closing_handoff_is_refused(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    """The driver double-taps "hand in 100 000": the first tap closes the trip, the second waited on its lock."""
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="first_tap",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _hand_in(pg_app, world, answers, errors, 100000, finally_set=holds, name="first_tap"),
        second=lambda: _hand_in(pg_app, world, answers, errors, 100000, name="second_tap"),
    )

    assert errors == []
    status, body = answers["first_tap"]
    assert (status, body["data"]["status"]) == (200, "submitted"), body
    next_session_id = body["data"]["next_active_session"]["id"]
    status, body = answers["second_tap"]
    assert status == 409, body
    assert (body["error"], body["error_code"]) == ("CONFLICT", "RECONCILIATION_SESSION_CHANGED")
    pg_db.session.expire_all()
    assert [(handoff.driver_cash_session_id, Decimal(str(handoff.amount))) for handoff in DriverCashHandoff.query] == [
        (world.session_id, Decimal("100000.00"))
    ]
    assert [active.id for active in _active_sessions(pg_db, world.driver_id)] == [next_session_id]


def test_a_return_behind_a_closing_handoff_finds_no_open_trip(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    world = _trip_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="handoff",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _hand_in(pg_app, world, answers, errors, 100000, finally_set=holds),
        second=lambda: _trip_return(pg_app, world, answers, errors, 50000),
    )

    assert errors == []
    status, body = answers["handoff"]
    assert (status, body["data"]["status"]) == (200, "submitted"), body
    status, body = answers["return"]
    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert body["errors"] == [
        "no_active_session: the driver has no open trip cash session; "
        "record it as 'driver, already settled' or 'office'"
    ]
    assert CustomerCreditPayout.query.count() == 0
    session = _session(pg_db, world.session_id)
    assert (
        session.status,
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.declared_cash)),
    ) == (DriverCashSessionStatus.SUBMITTED, Decimal("100000.00"), Decimal("100000.00"))
    assert len(_active_sessions(pg_db, world.driver_id)) == 1


# --------------------------------------------------------------------------- the same key, twice at once
#
# The admin double-taps Apply. Both requests miss the key before locking; the second waits on lock 1 behind the
# first. Once it holds the lock it must look for the key again: the first has drawn the whole free balance, so
# planning on would refuse it as a blocked return, though the return it asked for was recorded.


def test_the_same_key_twice_at_once_records_one_return_and_replays_it(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    customer_id, payment_id, headers = _world(pg_db, debt="10000.00")
    answers, errors = {}, []

    def tap(name, finally_set=None):
        return _thread(
            name,
            pg_app,
            lambda client: client.post(
                APPLY.format(customer_id), json=_return_body("k-double", amount=50000), headers=headers
            ),
            answers,
            errors,
            finally_set=finally_set,
        )

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="first_tap",
        first=lambda holds: tap("first_tap", finally_set=holds),
        second=lambda: tap("second_tap"),
    )

    assert errors == []
    first_status, first = answers["first_tap"]
    second_status, second = answers["second_tap"]
    assert (first_status, first["data"]["replayed"]) == (201, False), first
    assert (second_status, second["data"].get("error_code"), second["data"].get("replayed")) == (200, None, True)
    assert second["data"]["payout"] == first["data"]["payout"]
    pg_db.session.expire_all()
    assert [(p.id, Decimal(str(p.amount))) for p in CustomerCreditPayout.query.all()] == [
        (first["data"]["payout"]["id"], Decimal("50000.00"))
    ]
    assert _ledger(pg_db, customer_id, payment_id) == (Decimal("0.00"), Decimal("10000.00"))


# --------------------------------------------------------------------------- restore writers add in SQL
#
# A cancel hands the credit its order held back to the funding event, reading that event WITHOUT its lock
# (`release_pre_delivery_prepaid_settlement_for_order`, the same for every restore writer). The
# write must add to the row it finally updates, not write back a figure read before a return (or an undo)
# committed on the same event: that would hand the paid-out money back as spendable credit, or lose credit.

ADMIN_CANCEL = "/api/v1/admin/orders/{}/status"


def _held_credit_world(pg_db):
    """100 000 paid ahead at the office; a pending COD order of 30 000 holds 30 000 of it (covered, so settled
    ahead of delivery, which makes it read as paid: the customer may not cancel it himself, an admin does), and
    70 000 is free."""
    # The cancel releases stock reservations in Redis; `pg_app` is built without the suite's REDIS_URL
    # (the precedent of test_customer_cancel_races_pg.py).
    current_app.config["REDIS_URL"] = os.environ["REDIS_URL"]
    customer = _user(pg_db, "+998901261201", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    admin = _user(pg_db, "+998901261202", role=UserRole.ADMIN, user_type=UserType.STAFF)
    order, payment = delivered_cod_order(
        pg_db, customer, address=make_address(pg_db, customer), total=Decimal("30000.00"), status=OrderStatus.CONFIRMED
    )
    event = CashCollectionService().post_collection(
        customer_id=customer.id,
        amount=Decimal("100000.00"),
        source="admin_adjustment",
        recorded_by_user_id=admin.id,
        notes="Prepaid in cash at the office",
    )
    pg_db.session.commit()
    held = CashCollectionAllocation.query.filter_by(cash_collection_event_id=event.id).one()
    assert (Decimal(str(held.allocated_amount)), held.allocation_metadata.get("settled_pre_delivery")) == (
        Decimal("30000.00"),
        True,
    )
    assert Decimal(str(event.unapplied_amount)) == Decimal("70000.00")
    return SimpleNamespace(
        customer_id=customer.id,
        order_id=order.id,
        event_id=event.id,
        held_allocation_id=held.id,
        admin_headers={
            "Authorization": "Bearer "
            + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
            "Content-Type": "application/json",
        },
    )


def _admin_cancels(pg_app, world, answers, errors):
    """The Orders page's status change to Cancelled, on its own thread."""
    return _thread(
        "cancel",
        pg_app,
        lambda client: client.put(
            ADMIN_CANCEL.format(world.order_id),
            json={"status": "cancelled", "reason": "Customer changed their mind"},
            headers=world.admin_headers,
        ),
        answers,
        errors,
    )


def _event_after(pg_db, world):
    """(unapplied on the funding event, whether the order's held credit was handed back, the free balance)."""
    pg_db.session.expire_all()
    event = pg_db.session.get(CashCollectionEvent, world.event_id)
    held = pg_db.session.get(CashCollectionAllocation, world.held_allocation_id)
    return (
        Decimal(str(event.unapplied_amount)),
        held.reversed_at is not None,
        CashCollectionService().get_customer_prepaid_balance(world.customer_id),
    )


def test_a_cancel_behind_a_return_on_the_same_event_keeps_the_return(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    world = _held_credit_world(pg_db)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        first=lambda holds: _thread(
            "return",
            pg_app,
            lambda client: client.post(
                APPLY.format(world.customer_id),
                json=_return_body("k-cancel", amount=70000),
                headers=world.admin_headers,
            ),
            answers,
            errors,
            finally_set=holds,
        ),
        second=lambda: _admin_cancels(pg_app, world, answers, errors),
    )

    assert errors == []
    status, body = answers["return"]
    assert (status, body["data"]["payout"]["amount"]) == (201, 70000.0), body
    status, body = answers["cancel"]
    assert (status, body["data"]["order"]["status"]) == (200, "cancelled"), body
    # 100 000 = the 70 000 handed to the customer + the 30 000 the cancel gave back. Not 100 000 of credit
    # beside a 70 000 payout (the cancel writing back the 70 000 it read before the return committed).
    assert _event_after(pg_db, world) == (Decimal("30000.00"), True, Decimal("30000.00"))
    assert [(Decimal(str(p.amount)), p.voided_at) for p in CustomerCreditPayout.query.all()] == [
        (Decimal("70000.00"), None)
    ]
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


def test_a_cancel_behind_an_undo_on_the_same_event_loses_nothing(pg_app, pg_db, monkeypatch, enqueued):  # noqa: F811
    world = _held_credit_world(pg_db)
    returned = pg_app.test_client().post(
        APPLY.format(world.customer_id), json=_return_body("k-undo", amount=70000), headers=world.admin_headers
    )
    assert returned.status_code == 201, returned.get_json()
    payout_id = returned.get_json()["data"]["payout"]["id"]
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="undo",
        first=lambda holds: _thread(
            "undo",
            pg_app,
            lambda client: client.post(
                f"/api/v1/admin/staff/cash-reconciliation/prepayment-payouts/{payout_id}/undo",
                json={"reason": "Recorded by mistake"},
                headers=world.admin_headers,
            ),
            answers,
            errors,
            finally_set=holds,
        ),
        second=lambda: _admin_cancels(pg_app, world, answers, errors),
    )

    assert errors == []
    status, body = answers["undo"]
    assert (status, body["data"]["payout"]["undo_block_code"]) == (200, "already_undone"), body
    status, body = answers["cancel"]
    assert (status, body["data"]["order"]["status"]) == (200, "cancelled"), body
    # The undo's 70 000 and the cancel's 30 000 both land: the whole 100 000 is credit again. Not 30 000 (the
    # cancel writing 0 + 30 000 from before the undo committed).
    assert _event_after(pg_db, world) == (Decimal("100000.00"), True, Decimal("100000.00"))
    assert CustomerCreditPayout.query.one().voided_at is not None
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []


# --------------------------------------------------------------------------- a collection behind a trip return
#
# A collection refreshes the driver's session figures. Summed before a return from this trip's cash commits and
# written after it, they would store the on hand from before the return (MONEY-3): it sums under the row lock.


def test_a_collection_behind_a_trip_return_stores_the_lower_on_hand(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    world = _trip_world(pg_db)
    other = _user(pg_db, "+998901261104", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _trip_return(pg_app, world, answers, errors, 60000, finally_set=holds),
        second=lambda: _thread(
            "collection",
            pg_app,
            lambda client: client.post(
                "/api/v1/staff/cash-collections",
                json={"customer_id": other.id, "amount": 50000, "notes": "Paid at the door"},
                headers=world.driver_headers,
            ),
            answers,
            errors,
        ),
    )

    assert errors == []
    status, body = answers["return"]
    assert (status, body["data"]["driver_cash_session"]["expected_cash_on_hand"]) == (201, 40000.0), body
    status, body = answers["collection"]
    assert status in (200, 201), body
    session = _session(pg_db, world.session_id)
    # Collected 150 000 on this trip, 60 000 of it handed back to a customer: he holds 90 000, not 150 000.
    assert (
        session.status,
        Decimal(str(session.expected_cash)),
        Decimal(str(session.expected_cash_on_hand)),
    ) == (DriverCashSessionStatus.OPEN, Decimal("150000.00"), Decimal("90000.00"))


# The Celery overdue sweep refreshes every open session's figures and commits. Unlocked, it sums before a return
# from this trip's cash commits and writes after it: the stored on hand is the pre-return one (MONEY-3 residual).


def _overdue_sweep(pg_app, answers, errors):
    """`mark_overdue_sessions` as the Celery task runs it, a month on (every open trip is past its warning), on
    its own thread, app context, session and connection."""
    from datetime import datetime, timedelta, timezone

    def run():
        with pg_app.app_context():
            from business_app import db

            try:
                answers["overdue"] = DriverReconciliationService().mark_overdue_sessions(
                    reference_time=datetime.now(timezone.utc) + timedelta(days=30)
                )
            except Exception as exc:  # noqa: BLE001 -- named by the assertion that reads `errors`
                db.session.rollback()
                errors.append(f"overdue: {exc!r}")

    return threading.Thread(target=run, name="overdue")


def test_the_overdue_sweep_behind_a_trip_return_stores_the_lower_on_hand(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    world = _trip_world(pg_db)
    # The stored figure differs from the true one (any earlier drift), so the sweep's refresh has something to
    # write: a refresh that finds the stored value already right writes nothing and cannot show the race.
    pg_db.session.execute(
        text("UPDATE driver_cash_sessions SET expected_cash_on_hand = 70000 WHERE id = :s"), {"s": world.session_id}
    )
    pg_db.session.commit()
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=SESSION_LOCK,
        takes_the_lock=_for_update,
        first=lambda holds: _trip_return(pg_app, world, answers, errors, 60000, finally_set=holds),
        second=lambda: _overdue_sweep(pg_app, answers, errors),
    )

    assert errors == []
    status, body = answers["return"]
    assert (status, body["data"]["driver_cash_session"]["expected_cash_on_hand"]) == (201, 40000.0), body
    assert answers["overdue"] == 1
    session = _session(pg_db, world.session_id)
    # 100 000 collected on this trip, 60 000 of it handed back to the customer: he holds 40 000, not 100 000.
    assert (
        session.status,
        Decimal(str(session.expected_cash)),
        Decimal(str(session.expected_cash_on_hand)),
    ) == (DriverCashSessionStatus.OVERDUE, Decimal("100000.00"), Decimal("40000.00"))


# The two Celery sweeps lock each session row they refresh (above). They must hold one row at a time and release it
# before the next, and none while they message a driver: a driver's cash tap (`post_collection`, his hand-in) takes
# the same row lock, and a Telegram call can take 15 s.


def _drivers_on_trips(pg_db, *, overdue):
    """Two drivers on Telegram, each carrying 100 000 a customer paid him ahead on his current trip. `overdue`: both
    trips eight days old and past their warning, so the reminder run messages both; otherwise neither is due."""
    customer = _user(pg_db, "+998901261501", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    admin = _user(pg_db, "+998901261502", role=UserRole.ADMIN, user_type=UserType.STAFF)
    drivers = []
    for phone, telegram_id in (("+998901261503", "7000001"), ("+998901261504", "7000002")):
        driver = _user(pg_db, phone, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        driver.telegram_id = telegram_id
        pg_db.session.add(
            DeliveryPerson(user_id=driver.id, full_name="Race Driver", phone=driver.phone, is_active=True)
        )
        pg_db.session.commit()
        event = CashCollectionService().post_collection(
            customer_id=customer.id,
            amount=Decimal("100000.00"),
            source="standalone_meeting",
            collector_user_id=driver.id,
            recorded_by_user_id=admin.id,
            notes="Paid ahead to the driver",
        )
        pg_db.session.commit()
        drivers.append(
            SimpleNamespace(
                id=driver.id,
                session_id=event.driver_cash_session_id,
                headers={
                    "Authorization": "Bearer " + create_access_token(identity=str(driver.id)),
                    "Content-Type": "application/json",
                },
            )
        )
    if overdue:
        pg_db.session.execute(
            text(
                "UPDATE driver_cash_sessions SET session_started_at = now() - interval '8 days',"
                " warning_due_at = now() - interval '5 minutes', submission_due_at = now() - interval '5 minutes'"
                " WHERE id IN (:a, :b)"
            ),
            {"a": drivers[0].session_id, "b": drivers[1].session_id},
        )
        pg_db.session.commit()
    return drivers


def _row_lock(pg_db, session_id):
    """Whether a driver's cash tap could take his session row now: the lock `lock_session` takes (FOR NO KEY UPDATE),
    asked NOWAIT on a connection of its own. "held" when another transaction holds the row (SQLSTATE 55P03)."""
    with pg_db.engine.connect() as connection:
        try:
            connection.execute(
                text("SELECT id FROM driver_cash_sessions WHERE id = :s FOR NO KEY UPDATE NOWAIT"), {"s": session_id}
            )
            return "free"
        except OperationalError as exc:
            assert exc.orig.pgcode == "55P03", exc
            return "held"
        finally:
            connection.rollback()


def test_the_overdue_sweep_holds_one_session_row_at_a_time_and_none_once_done(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    """Nothing is due, so the sweep flips no session: each refresh must still release its row, not keep it until
    the task's app context ends (through the manager notifications that follow it)."""
    first, second = _drivers_on_trips(pg_db, overdue=False)
    probes = {}
    refresh = DriverReconciliationService.refresh_expected_cash
    notify = DriverReconciliationService.notify_managers_about_exception_sessions

    def refreshing(self, session):
        if session.id == second.session_id:
            probes["first, while the second is refreshed"] = _row_lock(pg_db, first.session_id)
        return refresh(self, session)

    def notifying(self):
        probes["both, once the sweep is done"] = tuple(_row_lock(pg_db, d.session_id) for d in (first, second))
        return notify(self)

    monkeypatch.setattr(DriverReconciliationService, "refresh_expected_cash", refreshing)
    monkeypatch.setattr(DriverReconciliationService, "notify_managers_about_exception_sessions", notifying)

    marked = payment_tasks.mark_overdue_cod_reconciliation_sessions.run()

    assert marked == {"updated_sessions": 0, "manager_notifications": 0}
    assert probes == {
        "first, while the second is refreshed": "free",
        "both, once the sweep is done": ("free", "free"),
    }
    assert [_session(pg_db, s).status for s in (first.session_id, second.session_id)] == [
        DriverCashSessionStatus.OPEN,
        DriverCashSessionStatus.OPEN,
    ]


def test_a_reminder_run_holds_no_session_row_while_telegram_answers(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    """The hourly reminder run as Celery runs it, with Telegram slow on the first driver's message. Meanwhile that
    driver records a payment at a door and the second driver hands in everything he holds: both taps go through at
    once, and the second driver, whose trip is now closed, is not reminded about it."""
    first, second = _drivers_on_trips(pg_db, overdue=True)
    other = _user(pg_db, "+998901261505", role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL)
    answers, errors, messaged, waited, taps = {}, [], [], {}, []
    real_telegram = NotificationService.send_staff_telegram_message

    def send_staff_telegram_message(self, *args, **kwargs):
        bound = inspect.signature(real_telegram).bind(self, *args, **kwargs)
        messaged.append(bound.arguments["user"].id)
        if len(messaged) == 1:
            taps.extend(
                [
                    _thread(
                        "collection",
                        pg_app,
                        lambda client: client.post(
                            "/api/v1/staff/cash-collections",
                            json={"customer_id": other.id, "amount": 50000, "notes": "Paid at the door"},
                            headers=first.headers,
                        ),
                        answers,
                        errors,
                    ),
                    _thread(
                        "handoff",
                        pg_app,
                        lambda client: client.post(HAND_IN, json={"declared_cash": None}, headers=second.headers),
                        answers,
                        errors,
                    ),
                ]
            )
            for tap in taps:
                tap.start()
            for tap in taps:
                tap.join(timeout=10)
            waited.update({tap.name: tap.is_alive() for tap in taps})
        return {"success": True}

    monkeypatch.setattr(NotificationService, "send_staff_telegram_message", send_staff_telegram_message)

    try:
        reminded = payment_tasks.send_cod_reconciliation_reminders.run()
    finally:
        _finish(*taps)

    assert waited == {"collection": False, "handoff": False}
    assert errors == []
    assert (answers["collection"][0], answers["handoff"][0]) == (201, 200), answers
    assert messaged == [first.id]
    assert (reminded["sent"], reminded["failed"]) == (1, 0)
    reminded_session, closed_session = _session(pg_db, first.session_id), _session(pg_db, second.session_id)
    # The reminder stamp leaves the door payment's figures alone: 100 000 + 50 000.
    assert (
        reminded_session.status,
        reminded_session.reminder_stage,
        reminded_session.last_reminder_at is not None,
        Decimal(str(reminded_session.expected_cash)),
        Decimal(str(reminded_session.expected_cash_on_hand)),
    ) == (DriverCashSessionStatus.OVERDUE, "overdue", True, Decimal("150000.00"), Decimal("150000.00"))
    assert (closed_session.status, closed_session.last_reminder_at, Decimal(str(closed_session.declared_cash))) == (
        DriverCashSessionStatus.SUBMITTED,
        None,
        Decimal("100000.00"),
    )


# --------------------------------------------------------------------------- lock 2 and a contract adjustment
#
# The manual contract adjustment reads the contract with its account, then locks the account to post. Behind a
# grocery store's return (lock 2), the lock must hand it the account as the return left it, or its posting
# overwrites the return's contract entry and the two ledgers disagree (MONEY-2).


def test_a_contract_adjustment_behind_a_grocery_return_posts_on_the_returned_balance(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    store = _user(pg_db, "+998901261301", role=UserRole.CUSTOMER, user_type=UserType.ENTITY)
    admin = _user(pg_db, "+998901261302", role=UserRole.ADMIN, user_type=UserType.STAFF)
    headers = {
        "Authorization": "Bearer " + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }
    contract, _account = _make_grocery_amount_contract(pg_db, store)
    contract_id = contract.id
    order, _payment = delivered_cod_order(
        pg_db, store, address=make_address(pg_db, store), total=Decimal("100000.00")
    )
    CorporateContractService().charge_on_delivery(order)
    pg_db.session.commit()
    collected = pg_app.test_client().post(
        "/api/v1/admin/staff/cash-reconciliation/collections",
        json={
            "customer_id": store.id,
            "amount": 130000,
            "source": "standalone_meeting",
            "notes": "Paid at the door",
            "order_id": order.id,
        },
        headers=headers,
    )
    assert collected.status_code == 201, collected.get_json()
    answers, errors = {}, []

    _race(
        pg_app,
        pg_db,
        monkeypatch,
        holder="return",
        lock=(CorporateContractService, "payout_mirror_plan"),
        takes_the_lock=lambda kwargs: bool(kwargs.get("lock")),
        first=lambda holds: _thread(
            "return",
            pg_app,
            lambda client: client.post(
                APPLY.format(store.id), json=_return_body("k-store", amount=30000), headers=headers
            ),
            answers,
            errors,
            finally_set=holds,
        ),
        second=lambda: _thread(
            "adjustment",
            pg_app,
            lambda client: client.post(
                f"/api/v1/admin/corporate/contracts/{contract_id}/adjustments",
                json={"amount": -10000, "reason": "Goodwill credit"},
                headers=headers,
            ),
            answers,
            errors,
        ),
    )

    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    status, body = answers["adjustment"]
    assert status == 201, body
    # -30 000 of contract credit, +30 000 handed back, then -10 000 goodwill: -10 000. Not -40 000 (the
    # adjustment posting on the -30 000 it read before the return's entry committed).
    assert body["data"]["ledger_entry"]["entry_metadata"] == {"outstanding_after": -10000.0}
    pg_db.session.expire_all()
    account = CorporatePrepaymentAccount.query.filter_by(contract_id=contract_id).one()
    assert Decimal(str(account.outstanding_amount)) == Decimal("-10000.00")
    assert sorted(
        Decimal(str(row.amount))
        for row in CorporatePrepaymentLedger.query.filter(
            CorporatePrepaymentLedger.contract_id == contract_id,
            CorporatePrepaymentLedger.idempotency_key.like("payout:%")
            | CorporatePrepaymentLedger.idempotency_key.like("adjustment:%"),
        )
    ) == [Decimal("-10000.00"), Decimal("30000.00")]


def _nowait(pg_db, table, where, **params):
    """Try `SELECT ... FOR UPDATE NOWAIT` on the rows of `table` matching `where`, from the test's own connection:
    "refused" when another transaction holds a row lock on them (SQLSTATE 55P03), else "acquired"."""
    try:
        pg_db.session.execute(text(f"SELECT id FROM {table} WHERE {where} FOR UPDATE NOWAIT"), params)
        return "acquired"
    except OperationalError as exc:
        assert exc.orig.pgcode == "55P03", exc
        return "refused"
    finally:
        pg_db.session.rollback()


def test_a_grocery_trip_return_holds_the_contract_account_before_it_takes_the_session_row(
    pg_app, pg_db, monkeypatch, enqueued  # noqa: F811
):
    """The lock order, observed from outside: with the return stopped right after lock 2 (the contract account),
    the credit events (lock 1) and the account are held, and the driver's session row (lock 3) is NOT yet."""
    store = _user(pg_db, "+998901261401", role=UserRole.CUSTOMER, user_type=UserType.ENTITY)
    admin = _user(pg_db, "+998901261402", role=UserRole.ADMIN, user_type=UserType.STAFF)
    driver = _user(pg_db, "+998901261403", role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    pg_db.session.add(DeliveryPerson(user_id=driver.id, full_name="Race Driver", phone=driver.phone, is_active=True))
    pg_db.session.commit()
    contract, _account = _make_grocery_amount_contract(pg_db, store)
    store_id, contract_id = store.id, contract.id
    order, _payment = delivered_cod_order(
        pg_db, store, address=make_address(pg_db, store), total=Decimal("100000.00")
    )
    CorporateContractService().charge_on_delivery(order)
    pg_db.session.commit()
    # 130 000 handed to the driver at the door for a 100 000 order: 30 000 of surplus, in his current trip cash.
    event = CashCollectionService().post_collection(
        customer_id=store_id,
        amount=Decimal("130000.00"),
        source="standalone_meeting",
        order_id=order.id,
        collector_user_id=driver.id,
        recorded_by_user_id=admin.id,
        notes="Paid at the door",
    )
    pg_db.session.commit()
    session_id = event.driver_cash_session_id
    assert session_id is not None
    headers = {
        "Authorization": "Bearer " + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }
    holds, release = threading.Event(), threading.Event()
    original = CorporateContractService.payout_mirror_plan

    def held(self, drawn, *, lock):
        result = original(self, drawn, lock=lock)
        if lock and threading.current_thread().name == "return" and not holds.is_set():
            holds.set()
            assert release.wait(timeout=30), "the test never released the return"
        return result

    monkeypatch.setattr(CorporateContractService, "payout_mirror_plan", held)
    answers, errors = {}, []
    thread = _thread(
        "return",
        pg_app,
        lambda client: client.post(
            APPLY.format(store_id),
            json={
                "amount": 30000,
                "paid_by": "driver_current_session",
                "driver_user_id": driver.id,
                "reason": "Store closed for good",
                "idempotency_key": "k-lock-2",
            },
            headers=headers,
        ),
        answers,
        errors,
        finally_set=holds,
    )
    thread.start()
    try:
        assert holds.wait(timeout=30), "the return never reached lock 2"
        probed = {
            "credit events (lock 1)": _nowait(
                pg_db, "cash_collection_events", "customer_id = :c AND unapplied_amount > 0", c=store_id
            ),
            "contract account (lock 2)": _nowait(
                pg_db, "corporate_prepayment_accounts", "contract_id = :c", c=contract_id
            ),
            "driver session (lock 3)": _nowait(pg_db, "driver_cash_sessions", "id = :s", s=session_id),
        }
    finally:
        release.set()
        _finish(thread)

    assert probed == {
        "credit events (lock 1)": "refused",
        "contract account (lock 2)": "refused",
        "driver session (lock 3)": "acquired",
    }
    assert errors == []
    status, body = answers["return"]
    assert status == 201, body
    assert body["data"]["payout"]["driver_cash_session_id"] == session_id
    assert reconcile_customer_link_invariants()["event_conservation_violations"] == []
