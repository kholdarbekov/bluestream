"""The nightly plan snapshot (R1): what each agent's day was SUPPOSED to be.

`outlets.next_visit_due_at` is rewritten every night by `sales.recompute_all_outlets`
for every non-lost outlet and has no history anywhere, so "how many shops were due
for this agent on the 16th" is answerable only for TODAY — and only until 01:00
tomorrow. Plan-vs-fact needs that denominator for past days, so one row per active
agent per local day is written at 01:20, after the recompute (01:00) and the stage
sweep (01:10) have finished moving the column it reads.

Frozen instants, handed in (`now=`), the digest/nightly-jobs contract. FROZEN_UTC is
2026-09-15 20:20 UTC = 2026-09-16 01:20 Tashkent, the job's own wall time: the local
date is the 16th while the UTC date is still the 15th, so a snapshot keyed on the UTC
day writes the wrong row and every assertion below says so.

Nothing here re-implements a rule: due membership is the due scope's own predicate,
and "overdue" is `serialize_sales.overdue_days`, the number the digest, the due-list
button and the outlet card already print.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import SalesAgentDayPlan
from business_app.serializers.sales_serializers import overdue_days
from business_app.services.sales.agent_account_service import SalesAgentAccountService
from business_app.services.sales.day_plan_service import AgentDayPlanService
from business_app.services.sales.digest_service import AgentDigestService
from business_app.services.sales.outlet_service import OutletService
from business_app.tasks import sales_agent_tasks
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import UserStatus
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.unit

FROZEN_UTC = datetime(2026, 9, 15, 20, 20, tzinfo=UTC)  # 01:20 Tashkent on the 16th
PLAN_DATE = date(2026, 9, 16)
LOCAL_DAY_END_UTC = datetime(2026, 9, 16, 19, 0, tzinfo=UTC)  # 00:00 Tashkent on the 17th
RECENT_VISIT_AT = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)


def _agent(db, phone, *, telegram_id=None, is_active=True, status=None):
    user = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    user.telegram_id = telegram_id
    if status is not None:
        user.status = status
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"], is_active=is_active))
    db.session.commit()
    return user


def _outlet(db, agent, name, *, stage="active", due_at=None, onboarded=False):
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage=stage,
        assigned_agent_user_id=None if onboarded else agent.id,
        onboarded_by_user_id=agent.id if onboarded else None,
        next_visit_due_at=due_at,
        last_visit_at=RECENT_VISIT_AT,
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _seed_a_days_work(db, agent):
    """Five shops due today, three of them already overdue — plus three that are not
    due at all, one on each of the three ways an outlet drops out of the scope.

    Every outlet here carries `last_visit_at=RECENT_VISIT_AT` (2026-09-12) while the counts are
    asserted at 2026-09-16 (M5): due-ness reads `next_visit_due_at`, so `last_visit_at` is
    deliberately uninvolved. `OutletService.unvisited_filter` — the OTHER predicate over the same
    column, which Task 5's `unvisited` exception reads — is untouched by this task and nothing here
    pins the two apart. Do not "align" them: due-ness is a schedule, unvisited-ness is a silence.
    """
    rows = {
        "overdue_five_days": _outlet(db, agent, "Bahor", due_at=datetime(2026, 9, 10, 4, 0, tzinfo=UTC)),
        "overdue_one_day": _outlet(db, agent, "Chorsu", due_at=datetime(2026, 9, 14, 20, 0, tzinfo=UTC)),
        # EXACTLY 24 hours past due. `overdue_days` floors a timedelta to whole days, so
        # this row is overdue by 1 — and the COUNT's SQL spelling (`due <= now - 1 day`)
        # agrees only while that comparison stays inclusive. Without a row sitting on the
        # boundary a `<=` -> `<` drift in the SQL would keep every assertion green.
        "overdue_exactly_one_day": _outlet(db, agent, "Chegara", due_at=FROZEN_UTC - timedelta(days=1)),
        "due_earlier_today": _outlet(db, agent, "Kecha", due_at=datetime(2026, 9, 15, 6, 0, tzinfo=UTC)),
        "due_late_tonight": _outlet(db, agent, "Bugun", due_at=LOCAL_DAY_END_UTC - timedelta(hours=1)),
    }
    # 00:30 LOCAL tomorrow — half an hour past the end of the agent's day, so it is
    # not today's work. A UTC-midnight cut-off would have swallowed it.
    _outlet(db, agent, "Ertaga", due_at=LOCAL_DAY_END_UTC + timedelta(minutes=30))
    _outlet(db, agent, "Sanasiz", due_at=None)
    _outlet(db, agent, "Yopilgan", stage="lost", due_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC))
    return rows


@pytest.fixture
def agent(db):
    return _agent(db, "+998901234631", telegram_id="777000431")


class TestDueCounts:
    def test_the_counts_are_the_due_scope_and_the_digests_own_overdue_split(self, db, agent):
        rows = _seed_a_days_work(db, agent)

        assert OutletService.due_counts(agent.id, now=FROZEN_UTC) == (5, 3)

        # ...and "overdue" is what every other surface prints — the serializer's
        # `overdue_days`, not a second date comparison living in the snapshot.
        assert {name: overdue_days(o.next_visit_due_at, FROZEN_UTC) > 0 for name, o in rows.items()} == {
            "overdue_five_days": True,
            "overdue_one_day": True,
            "overdue_exactly_one_day": True,
            "due_earlier_today": False,
            "due_late_tonight": False,
        }

    def test_the_digest_prints_the_same_total_it_snapshots(self, db, agent, monkeypatch):
        """One helper, two readers.

        The digest's "+N more" and the snapshot's `due_count` are the same number;
        computing them separately is how a manager's plan-vs-fact denominator and
        the agent's own morning message end up disagreeing about the same day.
        `DUE_PAGE_SIZE` is shrunk rather than seeding 101 outlets (the digest
        test's own technique), so the page-overflow term is exercised too.
        """
        for index in range(25):
            _outlet(
                db,
                agent,
                f"Qarzdor {index:02d}",
                due_at=datetime(2026, 8, 20, 4, 0, tzinfo=UTC) + timedelta(days=index),
            )
        monkeypatch.setattr(AgentDigestService, "DUE_PAGE_SIZE", 12)

        due_count, overdue_count = OutletService.due_counts(agent.id, now=FROZEN_UTC)
        payload = AgentDigestService.build(agent.id, now=FROZEN_UTC)

        assert (due_count, overdue_count) == (25, 25)
        named = len(payload["due_today"]) + len(payload["overdue"])
        assert named == 10
        assert named + payload["more_count"] == due_count

    def test_an_onboarded_outlet_counts_even_after_the_territory_moves(self, db, agent):
        """`agent_outlet_filter` is assigned-OR-onboarded, and the plan must be the
        same estate the agent's own due list shows them."""
        _outlet(db, agent, "Onboarded", due_at=datetime(2026, 9, 10, 4, 0, tzinfo=UTC), onboarded=True)

        assert OutletService.due_counts(agent.id, now=FROZEN_UTC) == (1, 1)


class TestSnapshotAll:
    def test_one_row_per_active_agent_for_the_local_day_the_job_is_standing_in(self, db, agent):
        _seed_a_days_work(db, agent)
        quiet = _agent(db, "+998901234632")  # no Telegram chat, nothing due

        assert AgentDayPlanService.snapshot_all(now=FROZEN_UTC) == 2

        rows = {row.agent_user_id: row for row in SalesAgentDayPlan.query.all()}
        assert set(rows) == {agent.id, quiet.id}
        assert (rows[agent.id].plan_date, rows[agent.id].due_count, rows[agent.id].overdue_count) == (
            PLAN_DATE,
            5,
            3,
        )
        # A day with nothing due is a row of ZEROES, never a missing row: "no plan
        # was taken" and "there was nothing to do" are different answers and
        # plan-vs-fact prints them differently (`plan_source`). An agent with no
        # linked chat gets no digest but still gets a plan — the two questions are
        # not the same, which is why this loop is not the digest's.
        assert (rows[quiet.id].due_count, rows[quiet.id].overdue_count) == (0, 0)
        assert ensure_utc(rows[quiet.id].snapshot_at) == FROZEN_UTC

    def test_the_plan_date_is_the_local_day_not_the_utc_one(self, db, agent):
        """The job fires at 01:20 Tashkent, when UTC is still on yesterday's date:
        a snapshot keyed on the UTC day would file every night's plan against the
        day before and shift plan-vs-fact by one row, forever."""
        AgentDayPlanService.snapshot_all(now=FROZEN_UTC)

        assert SalesAgentDayPlan.query.one().plan_date == PLAN_DATE == date(2026, 9, 16)

    def test_a_second_run_updates_the_row_instead_of_adding_one(self, db, agent):
        """Re-running the job AFTER THE FIRST RUN HAS FINISHED (a retry, a manual
        `celery call`) must not double the denominator — the unique
        `(agent_user_id, plan_date)` is the backstop and the sequential upsert is what
        keeps it from raising. Two OVERLAPPING runs would still collide on that
        constraint; beat does not produce that overlap."""
        _seed_a_days_work(db, agent)
        AgentDayPlanService.snapshot_all(now=FROZEN_UTC)
        Outlet.query.filter_by(name="Bahor").one().next_visit_due_at = None
        db.session.commit()
        later = FROZEN_UTC + timedelta(minutes=3)

        assert AgentDayPlanService.snapshot_all(now=later) == 1

        row = SalesAgentDayPlan.query.one()
        assert (row.plan_date, row.due_count, row.overdue_count) == (PLAN_DATE, 4, 2)
        assert ensure_utc(row.snapshot_at) == later

    def test_a_deactivated_agent_and_a_suspended_user_get_no_plan(self, db, agent):
        _agent(db, "+998901234633", is_active=False)
        _agent(db, "+998901234634", status=UserStatus.INACTIVE)

        assert AgentDayPlanService.snapshot_all(now=FROZEN_UTC) == 1

        assert [row.agent_user_id for row in SalesAgentDayPlan.query.all()] == [agent.id]
        # ...and it is ONE definition of "an agent the business expects to work",
        # the same list the *Sales agents* summary card counts and the same list
        # Task 2's metrics rows report on.
        assert [row.id for row in SalesAgentAccountService.active_agents()] == [agent.id]


class TestSnapshotTaskWiring:
    def test_the_task_runs_the_service_and_reports_the_count(self, db, agent):
        """A thin shell over the service, the `recompute_all_outlets` precedent
        (`tests/unit/test_sales_nightly_jobs.py:383`). It passes no `now`, so this
        asserts the COUNT, not a calendar day."""
        _seed_a_days_work(db, agent)

        assert sales_agent_tasks.snapshot_agent_day_plans.run() == {"success": True, "agents": 1}
        assert SalesAgentDayPlan.query.count() == 1
