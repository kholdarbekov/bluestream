"""Postgres-only proofs of the ledger sync's backstops (spec §4.4.4, §4.15, §10.5).

The SQLite suite cannot show these: `FOR UPDATE` is a no-op there, and pysqlite turns a
SAVEPOINT that opens a transaction into a transaction of its own. Here the migrations are
real, so:
- T-SYNC-3: the unique idempotency key refuses a duplicate line, by name;
- T-SYNC-4: two syncs of one agent racing behind a barrier serialize on the open period
  rows. The second waits, then reads the committed credit and writes nothing: no conflict
  and no 500. That separates the period lock from the unique key, which would instead show
  up as a conflict;
- a run that read the posted lines before a winner committed (simulated by hiding them)
  loses the key race: only THAT order's SAVEPOINT rolls back, it is counted as a conflict,
  and the agent's other order is still credited.
"""

import logging
import threading
from datetime import date, datetime, timezone

import pytest

from business_app.models.sales_pay import SalesPayLedgerLine
from business_app.models.user import User
from business_app.services.sales import pay_ledger_service
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.utils.exceptions import ConflictError
from tests.integration.sales_pay_builders import pay_on_postgres, pg_agent_order
from tests.integration.test_sales_agent_pg_constraints import _rollback_on_named_integrity

pytestmark = pytest.mark.integration

# The pay world (`pay_on_postgres`, `pg_agent_order`, PG_SETUP / PG_DELIVERED / PG_PAID) is
# the builders' Task 5 section (plan ruling PR5); only these two instants are this module's.
OCTOBER = date(2026, 10, 1)
SYNC_AT = datetime(2026, 10, 5, 5, 0, tzinfo=timezone.utc)
NOVEMBER_2 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 10:00 local, past October's closable_from


def _admin():
    return User.query.filter_by(phone="+998901234568").one()


def test_a_duplicate_idempotency_key_is_refused_by_name(pg_db):
    """T-SYNC-3."""
    agent, water = pay_on_postgres(pg_db)
    pg_agent_order(pg_db, agent, water, "Bahor")
    SalesPayLedgerService.sync([agent.id], now=SYNC_AT)
    line = SalesPayLedgerLine.query.one()

    duplicate = SalesPayLedgerLine(
        agent_user_id=line.agent_user_id,
        period_id=line.period_id,
        kind=line.kind,
        cause=None,
        order_id=line.order_id,
        plan_version_id=line.plan_version_id,
        amount=line.amount,
        earned_month=line.earned_month,
        occurred_at=line.occurred_at,
        idempotency_key=line.idempotency_key,
        snapshot=dict(line.snapshot),
    )

    _rollback_on_named_integrity(pg_db, duplicate, "uq_sales_pay_ledger_lines_idempotency_key")


def test_two_syncs_of_one_agent_serialize_on_the_open_periods(pg_app, pg_db):
    """T-SYNC-4. Each thread has its own app context and session."""
    agent, water = pay_on_postgres(pg_db)
    order = pg_agent_order(pg_db, agent, water, "Bahor")
    agent_id, order_id = agent.id, order.id
    pg_db.session.commit()

    barrier = threading.Barrier(2, timeout=30)
    results, errors = [], []

    def worker():
        with pg_app.app_context():
            from business_app import db as thread_db

            try:
                barrier.wait()
                results.append(SalesPayLedgerService.sync([agent_id], now=SYNC_AT))
            except Exception as exc:  # pragma: no cover - asserted below
                thread_db.session.rollback()
                errors.append(exc)
            finally:
                thread_db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert sorted(run.credited for run in results) == [0, 1]
    assert [run.conflicts for run in results] == [0, 0]
    assert [run.failed for run in results] == [[], []]
    pg_db.session.expire_all()
    assert [(line.kind, line.amount, line.idempotency_key) for line in SalesPayLedgerLine.query.all()] == [
        ("commission_credit", None, f"commission:{order_id}:1")
    ]


def test_a_lost_key_race_rolls_back_only_that_order(pg_db, monkeypatch):
    """The SAVEPOINT per order (§4.4.4). The first sync credits the first order. The second
    run is shown a ledger without that credit, as a run that read before the winner committed
    would be: it computes the same slot, the unique key refuses it, that order's SAVEPOINT
    rolls back and counts one conflict, and the second order is still credited."""
    agent, water = pay_on_postgres(pg_db)
    first = pg_agent_order(pg_db, agent, water, "Bahor")
    SalesPayLedgerService.sync([agent.id], now=SYNC_AT)
    second = pg_agent_order(pg_db, agent, water, "Chorsu")
    real_posted = pay_ledger_service._posted_lines_by_order

    def stale_posted(agent_user_id, order_ids):
        posted = real_posted(agent_user_id, order_ids)
        posted.pop(first.id, None)
        return posted

    monkeypatch.setattr(pay_ledger_service, "_posted_lines_by_order", stale_posted)
    stats = SalesPayLedgerService.sync([agent.id], now=SYNC_AT)

    assert (stats.conflicts, stats.credited, stats.failed) == (1, 1, [])
    pg_db.session.expire_all()
    keys = sorted(line.idempotency_key for line in SalesPayLedgerLine.query.all())
    assert keys == sorted([f"commission:{first.id}:1", f"commission:{second.id}:1"])

    # Final-review M3: a close that meets only a lost race names no order, so it says why.
    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService.close(OCTOBER, actor_id=_admin().id, now=NOVEMBER_2)
    assert refused.value.error_code == "SALES_PAY_SYNC_INCOMPLETE"
    assert refused.value.details == {"orders": [], "agents": [], "reason": "concurrent"}


def test_a_constraint_that_is_not_a_slot_race_fails_the_order_by_name(pg_db, monkeypatch, caplog):
    """Final-review M3. Only the idempotency key is a lost race. Any other IntegrityError (here
    the cause CHECK, by a credit written with a cause) is a fault in THAT order: it is logged, listed in
    `failed` with its order id, counts no conflict, and a close names the order instead of
    "Some orders could not be synced ()"."""
    agent, water = pay_on_postgres(pg_db)
    broken = pg_agent_order(pg_db, agent, water, "Bahor")
    healthy = pg_agent_order(pg_db, agent, water, "Chorsu")
    real_append = pay_ledger_service.SyncContext.append_line

    def caused_for_broken(self, order, state, **fields):
        if order.id == broken.id:
            fields["cause"] = "not_delivered"  # a credit with a cause: ck_sales_pay_ledger_lines_cause refuses it
        return real_append(self, order, state, **fields)

    monkeypatch.setattr(pay_ledger_service.SyncContext, "append_line", caused_for_broken)
    # The module logger propagates into a `business_app.*` logger with `propagate=False`, so the
    # handler goes on the module's own logger (`logged_push_failures`' pattern).
    pay_ledger_service.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger=pay_ledger_service.logger.name):
            stats = SalesPayLedgerService.sync([agent.id], now=SYNC_AT)
    finally:
        pay_ledger_service.logger.removeHandler(caplog.handler)

    assert (stats.conflicts, stats.credited) == (0, 1)
    assert stats.failed == [{"agent_id": agent.id, "order_id": broken.id, "error": "IntegrityError"}]
    assert [record.getMessage() for record in caplog.records if record.exc_info] == [
        f"Sales pay sync failed for order {broken.id}"
    ]
    pg_db.session.expire_all()
    assert [line.order_id for line in SalesPayLedgerLine.query.all()] == [healthy.id]

    with pytest.raises(ConflictError) as refused:
        SalesPayPeriodService.close(OCTOBER, actor_id=_admin().id, now=NOVEMBER_2)
    assert refused.value.error_code == "SALES_PAY_SYNC_INCOMPLETE"
    assert refused.value.details == {"orders": [broken.id], "agents": [agent.id]}
