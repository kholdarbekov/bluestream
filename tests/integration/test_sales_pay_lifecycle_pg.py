"""Postgres-only proofs of the month lifecycle (§4.9, §4.15, §10.5).

`FOR UPDATE` is a no-op on SQLite, so these run on `pg_db`, on the real migrations:
- close with two agents, then recalculate: the status is `closed` before any statement is
  frozen, and the unique (period, agent) key leaves one statement per agent, re-frozen in place;
- an adjustment into a closed month re-freezes it in the same transaction (no trigger to trip);
- a close holding October's row makes a concurrent sync wait; once the close commits, the sync
  finds October closed and routes its line late into November;
- a plan version for October racing October's close waits on the same row and is refused.

The pay setup is Task 5's (the builders' `pay_on_postgres`): plan "Standard" v1 from October, one agent,
pay started in October. `now` is always passed.
"""

import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from business_app.models.sales_pay import SalesPayLedgerLine, SalesPayPeriod, SalesPayPlan, SalesPayPlanVersion, SalesPayStatement
from business_app.models.sales import SalesAgentProfile
from business_app.models.user import User
from business_app.serializers.sales_pay_serializers import VersionPayload
from business_app.services.sales.pay_ledger_service import SalesPayLedgerService
from business_app.services.sales.pay_penalty_service import SalesPayAdjustmentService
from business_app.services.sales.pay_period_service import SalesPayPeriodService
from business_app.services.sales.pay_plan_service import SalesPayPlanService
from business_app.services.sales.pay_statement_service import SalesPayStatementService
from business_app.services.sales.pay_terms_service import SalesPayTermsService
from business_app.utils.exceptions import ConflictError
from tests.integration.sales_pay_builders import (
    OCT,
    PG_SETUP,
    make_agent_order,
    make_outlet,
    make_store,
    pay_on_postgres,
    pg_agent_order,
    version_payload,
)
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

NOVEMBER_2 = datetime(2026, 11, 2, 5, 0, tzinfo=timezone.utc)  # 10:00 local, past October's closable_from
OCTOBER_31_PAID = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)  # 17:00 local: earned in October


def _admin():
    return User.query.filter_by(phone="+998901234568").one()


def _second_agent(pg_db):
    agent = make_sales_agent_user(pg_db, phone="+998901234599", staff_roles=["sales_agent"])
    pg_db.session.add(SalesAgentProfile(user_id=agent.id, districts=["yunusobod"]))
    pg_db.session.commit()
    admin = _admin()
    SalesPayTermsService.set_employment(agent.id, start=OCT, end=None, actor_id=admin.id, now=PG_SETUP)
    SalesPayTermsService.add_terms(
        agent.id,
        effective_month=OCT,
        base_salary=Decimal("4000000"),
        plan_id=SalesPayPlan.query.one().id,
        note=None,
        actor_id=admin.id,
        now=PG_SETUP,
    )
    return agent


def _pause_freezes(monkeypatch):
    """Make every freeze wait for the test: `started` is set once the close holds October's row
    lock and has flushed `closed`; the freeze goes on only when `release` is set."""
    started, release = threading.Event(), threading.Event()
    real_freeze = SalesPayStatementService.freeze

    def paused(period, agent_user_id, *, actor_id, now):
        started.set()
        release.wait(timeout=30)
        return real_freeze(period, agent_user_id, actor_id=actor_id, now=now)

    monkeypatch.setattr(SalesPayStatementService, "freeze", staticmethod(paused))
    return started, release


def _in_thread(pg_app, target, outcome):
    def run():
        with pg_app.app_context():
            from business_app import db as thread_db

            try:
                outcome["result"] = target()
            except Exception as exc:  # noqa: BLE001 - asserted by the test
                thread_db.session.rollback()
                outcome["error"] = exc
            finally:
                thread_db.session.remove()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_close_with_two_agents_freezes_after_the_status_and_recalculate_refreezes_in_place(pg_db, monkeypatch):
    """Both agents are frozen at revision 1 by the close, and every freeze saw its period
    already `closed` (status before freeze, §4.9). Recalculate re-freezes both in place:
    revision 2, still one statement per (period, agent)."""
    agent, water = pay_on_postgres(pg_db)
    second = _second_agent(pg_db)
    pg_agent_order(pg_db, agent, water, "Bahor")
    pg_agent_order(pg_db, second, water, "Chorsu")
    seen = []
    real_freeze = SalesPayStatementService.freeze

    def watched(period, agent_user_id, *, actor_id, now):
        seen.append(period.status)
        return real_freeze(period, agent_user_id, actor_id=actor_id, now=now)

    monkeypatch.setattr(SalesPayStatementService, "freeze", staticmethod(watched))
    admin = _admin()

    closed = SalesPayPeriodService.close(OCT, actor_id=admin.id, now=NOVEMBER_2)
    first = sorted((s.agent_user_id, s.revision, int(s.commission_amount)) for s in SalesPayStatement.query.all())
    SalesPayPeriodService.recalculate(OCT, actor_id=admin.id, now=NOVEMBER_2)
    pg_db.session.expire_all()
    second_pass = sorted((s.agent_user_id, s.revision) for s in SalesPayStatement.query.all())

    assert (closed.status, closed.closed_by_user_id) == ("closed", admin.id)
    assert seen == ["closed", "closed", "closed", "closed"]
    assert first == sorted([(agent.id, 1, 9000), (second.id, 1, 9000)])
    assert second_pass == sorted([(agent.id, 2), (second.id, 2)])


def test_an_adjustment_into_a_closed_month_refreezes_it(pg_db):
    agent, water = pay_on_postgres(pg_db)
    pg_agent_order(pg_db, agent, water, "Bahor")
    admin = _admin()
    SalesPayPeriodService.close(OCT, actor_id=admin.id, now=NOVEMBER_2)

    adjustment = SalesPayAdjustmentService.create(
        agent.id, OCT, Decimal("50000"), "Guarantee top-up", actor_id=admin.id, now=NOVEMBER_2
    )

    pg_db.session.expire_all()
    statement = SalesPayStatement.query.one()
    assert (adjustment.period.status, statement.revision, int(statement.adjustment_amount)) == ("closed", 2, 50000)
    assert int(statement.total_amount) == 5000000 + 9000 + 50000


def test_a_close_holding_the_period_lock_makes_a_sync_wait_then_post_late(pg_app, pg_db, monkeypatch):
    """The close syncs (crediting the first order into October), locks October, flushes `closed`
    and pauses in its first freeze. A second order earned on 31 October is created meanwhile,
    and a sync starts: it waits on October's row. When the close commits, the sync re-reads
    October as closed and posts the new credit late into November. October's statement holds
    only the first order."""
    agent, water = pay_on_postgres(pg_db)
    pg_agent_order(pg_db, agent, water, "Bahor")
    admin_id, agent_id = _admin().id, agent.id
    started, release = _pause_freezes(monkeypatch)
    close_outcome, sync_outcome = {}, {}

    closer = _in_thread(
        pg_app, lambda: SalesPayPeriodService.close(OCT, actor_id=admin_id, now=NOVEMBER_2).status, close_outcome
    )
    assert started.wait(timeout=30)
    outlet = make_outlet(pg_db, onboarded_by=agent, created_at=PG_SETUP, user=make_store(pg_db, name="Chorsu"))
    late = make_agent_order(pg_db, agent, outlet, [(water, 6)], delivered_at=OCTOBER_31_PAID, paid_at=OCTOBER_31_PAID)
    late_id = late.id
    syncer = _in_thread(pg_app, lambda: SalesPayLedgerService.sync([agent_id], now=NOVEMBER_2), sync_outcome)
    syncer.join(timeout=1.0)
    waited = syncer.is_alive()
    release.set()
    closer.join(timeout=60)
    syncer.join(timeout=60)

    pg_db.session.expire_all()
    [line] = SalesPayLedgerLine.query.filter_by(order_id=late_id).all()
    statement = SalesPayStatement.query.one()
    assert waited, "the sync did not wait for the close's lock on October"
    assert (close_outcome.get("error"), close_outcome.get("result")) == (None, "closed")
    assert sync_outcome.get("error") is None and sync_outcome["result"].credited == 1
    assert (line.earned_month, line.period.month_start) == (OCT, datetime(2026, 11, 1).date())
    assert SalesPayPeriodService.is_late(line.earned_month, line.period) is True
    assert int(statement.commission_amount) == 9000


def test_a_plan_version_racing_a_close_of_its_month_is_refused(pg_app, pg_db, monkeypatch):
    """`assert_month_editable` locks October's row (review money M2): the version waits for the
    close, re-reads October as closed and is refused with SALES_PAY_MONTH_LOCKED. No version
    row is written."""
    agent, water = pay_on_postgres(pg_db)
    pg_agent_order(pg_db, agent, water, "Bahor")
    admin_id = _admin().id
    plan_id = SalesPayPlan.query.one().id
    version = VersionPayload.model_validate(version_payload("2026-10", water.id, water_rate=2000)).model_dump(
        exclude_unset=True
    )
    started, release = _pause_freezes(monkeypatch)
    close_outcome, version_outcome = {}, {}

    closer = _in_thread(
        pg_app, lambda: SalesPayPeriodService.close(OCT, actor_id=admin_id, now=NOVEMBER_2).status, close_outcome
    )
    assert started.wait(timeout=30)
    publisher = _in_thread(
        pg_app,
        lambda: SalesPayPlanService.create_version(plan_id, version, actor_id=admin_id, now=NOVEMBER_2),
        version_outcome,
    )
    publisher.join(timeout=1.0)
    waited = publisher.is_alive()
    release.set()
    closer.join(timeout=60)
    publisher.join(timeout=60)

    pg_db.session.expire_all()
    assert waited, "the version did not wait for the close's lock on October"
    assert close_outcome.get("result") == "closed"
    assert isinstance(version_outcome.get("error"), ConflictError)
    assert version_outcome["error"].error_code == "SALES_PAY_MONTH_LOCKED"
    assert SalesPayPlanVersion.query.count() == 1
    assert SalesPayPeriod.query.filter_by(month_start=OCT).one().status == "closed"
