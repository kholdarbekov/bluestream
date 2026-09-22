"""/api/v1/admin/sales/agents/**/metrics driven the way the Analytics tab drives it.

A frozen clock, not a seeded "now": both routes resolve their window from the wall clock
(there is no `now=` to hand in over HTTP), so what is pinned here is the ONE name every
phase-3 window reads -- `local_windows.local_now`. 2026-09-17 08:30 Tashkent is 03:30 UTC
and the local day it names began at 2026-09-16 19:00 UTC, so every boundary below is a
real local-midnight boundary and not an artefact of a UTC-midnight comparison: the visit
seeded at 2026-09-16 19:30 UTC is TODAY's, and the one at 2026-08-31 18:00 UTC is outside
a month window that starts on 2026-09-01.

Nothing here re-implements a metric. The numbers asserted are `completed_visits`, whose
rule is AgentMetricsService's (Task 2, unit-tested there against all twenty keys); what
this file proves is that the ROUTES resolve the right window, scope to the right agent,
publish the twenty keys the tab and the email read, and refuse a bad range with one code.

What an HTTP body can pin about those keys is MEMBERSHIP, not order: `app.json.sort_keys`
is True estate-wide, so Flask re-orders every dict alphabetically on the way out. The
reading ORDER lives in `METRIC_KEYS` itself -- which the bot card, the email table and the
CSV header walk directly -- and is pinned where it is produced, in
tests/unit/test_agent_metrics_service.py.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import Visit
from business_app.services.sales.agent_metrics_service import METRIC_KEYS
from business_app.utils import local_windows
from shared.constants import DISPLAY_TIMEZONE
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

LIST_URL = "/api/v1/admin/sales/agents/metrics"

# Thursday 2026-09-17, 08:30 Tashkent = 03:30 UTC. Its week is Mon 09-14..Thu 09-17 and its
# month is 09-01..09-17, so `today`, `week` and `month` are three DIFFERENT windows.
FROZEN_LOCAL = datetime(2026, 9, 17, 8, 30, tzinfo=ZoneInfo(DISPLAY_TIMEZONE))

# Every instant is written in UTC so the local-midnight boundary is visible in the literal.
TODAY_00_30 = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)  # local 2026-09-17 00:30
TUESDAY_12_00 = datetime(2026, 9, 15, 7, 0, tzinfo=timezone.utc)  # local 2026-09-15 12:00
LAST_TUESDAY_12_00 = datetime(2026, 9, 8, 7, 0, tzinfo=timezone.utc)  # local 2026-09-08 12:00
AUGUST_23_00 = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)  # local 2026-08-31 23:00


@pytest.fixture
def frozen_local_now(monkeypatch):
    """Freeze the clock `local_windows` reads.

    The module binds `local_now` by name, so patching the name IN THAT MODULE freezes
    `local_date`, `period_window` and `parse_date_range`'s default window together.
    Patching `delivery_window.local_now` instead would miss all three: the name is
    already bound, so the defaults would drift back to the real wall clock and every
    window assertion below would pass or fail by the calendar rather than by the code.
    """
    monkeypatch.setattr(local_windows, "local_now", lambda: FROZEN_LOCAL)
    return FROZEN_LOCAL


def _agent(db, *, phone, first_name, is_active=True):
    user = make_sales_agent_user(db, phone=phone, staff_roles=["sales_agent"])
    user.first_name = first_name
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"], is_active=is_active))
    db.session.commit()
    return user


def _outlet(db, agent, name):
    outlet = Outlet(name=name, outlet_type="grocery_store", stage="active", assigned_agent_user_id=agent.id)
    db.session.add(outlet)
    db.session.commit()
    return outlet


def _completed_visit(db, agent, outlet, started_at):
    visit = Visit(
        outlet_id=outlet.id,
        agent_user_id=agent.id,
        status="completed",
        planned=True,
        current_step="close",
        started_at=started_at,
        ended_at=started_at + timedelta(minutes=20),
        outcome="no_order",
    )
    db.session.add(visit)
    db.session.commit()
    return visit


@pytest.fixture
def senior(db):
    """Four completed visits: today, Tuesday, last Tuesday, and one in August."""
    agent = _agent(db, phone="+998901234573", first_name="Sardor")
    outlet = _outlet(db, agent, "Bahor market")
    for started_at in (TODAY_00_30, TUESDAY_12_00, LAST_TUESDAY_12_00, AUGUST_23_00):
        _completed_visit(db, agent, outlet, started_at)
    return agent


@pytest.fixture
def junior(db):
    """Two completed visits, both today, at two different shops."""
    agent = _agent(db, phone="+998901234574", first_name="Aziz")
    for name in ("Sunnat do'kon", "Chorsu rastasi"):
        _completed_visit(db, agent, _outlet(db, agent, name), TODAY_00_30)
    return agent


@pytest.fixture
def retired(db):
    """A deactivated agent who worked today: the list route must not carry them."""
    agent = _agent(db, phone="+998901234575", first_name="Zafar", is_active=False)
    _completed_visit(db, agent, _outlet(db, agent, "Yopilgan"), TODAY_00_30)
    return agent


def test_the_list_route_answers_for_every_active_agent_in_one_pass(
    client, admin_claim_headers, db, frozen_local_now, senior, junior, retired
):
    """One request, one row per ACTIVE agent, sorted by name -- the Agent-performance tab
    and the Sales-agents cards both draw off this, and neither may make N calls."""
    resp = client.get(
        LIST_URL,
        headers=admin_claim_headers,
        query_string={"start_date": "2026-09-17", "end_date": "2026-09-17"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert (data["start_date"], data["end_date"]) == ("2026-09-17", "2026-09-17")

    rows = data["agents"]
    assert [row["agent_name"] for row in rows] == ["Aziz Agent", "Sardor Agent"]
    assert [row["agent_user_id"] for row in rows] == [junior.id, senior.id]
    assert [row["phone"] for row in rows] == ["+998901234574", "+998901234573"]
    # Zafar worked today and is deactivated: absent above, and absent here.
    assert retired.id not in [row["agent_user_id"] for row in rows]
    assert {row["agent_name"]: row["completed_visits"] for row in rows} == {"Aziz Agent": 2, "Sardor Agent": 1}
    for row in rows:
        assert set(row) == {"agent_user_id", "agent_name", "phone", *METRIC_KEYS}


def test_the_default_window_is_the_last_seven_local_days_and_a_range_is_inclusive(
    client, admin_claim_headers, db, frozen_local_now, senior, junior
):
    """No dates = 2026-09-11..2026-09-17, which drops Sardor's 09-08 visit; the September
    window keeps it and still drops the one at local 2026-08-31 23:00."""
    default = client.get(LIST_URL, headers=admin_claim_headers)
    assert default.status_code == 200, default.get_data(as_text=True)
    body = default.get_json()["data"]
    assert (body["start_date"], body["end_date"]) == ("2026-09-11", "2026-09-17")
    assert {row["agent_name"]: row["completed_visits"] for row in body["agents"]} == {
        "Aziz Agent": 2,
        "Sardor Agent": 2,
    }

    month = client.get(
        LIST_URL,
        headers=admin_claim_headers,
        query_string={"start_date": "2026-09-01", "end_date": "2026-09-17"},
    )
    assert month.status_code == 200, month.get_data(as_text=True)
    assert {row["agent_name"]: row["completed_visits"] for row in month.get_json()["data"]["agents"]} == {
        "Aziz Agent": 2,
        "Sardor Agent": 3,
    }


def test_one_agents_card_carries_the_twenty_keys_and_the_window_it_was_measured_over(
    client, admin_claim_headers, db, frozen_local_now, senior, junior
):
    resp = client.get(
        f"/api/v1/admin/sales/agents/{senior.id}/metrics",
        headers=admin_claim_headers,
        query_string={"start_date": "2026-09-01", "end_date": "2026-09-17"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["agent"] == {"id": senior.id, "full_name": "Sardor Agent"}
    assert (data["start_date"], data["end_date"]) == ("2026-09-01", "2026-09-17")
    # The twenty names are the service's to decide, not this route's: the tab's columns, the
    # weekly email's rows and the bot's card are all drawn from that one tuple.
    assert set(data["metrics"]) == set(METRIC_KEYS)
    assert data["metrics"]["completed_visits"] == 3  # Aziz's two visits are not on Sardor's card


def test_a_user_who_is_not_a_sales_agent_is_a_404_not_an_empty_card(
    client, admin_claim_headers, db, frozen_local_now, admin_user, senior
):
    """The path id is a users.id, and "is this id an agent" is answered ONCE, by
    SalesAgentAccountService.get_agent -- a second predicate here is how a customer id
    renders twenty zeroes and looks like a lazy agent."""
    resp = client.get(f"/api/v1/admin/sales/agents/{admin_user.id}/metrics", headers=admin_claim_headers)
    assert resp.status_code == 404, resp.get_data(as_text=True)
    assert resp.get_json()["error_code"] == "STAFF_USER_NOT_FOUND"


@pytest.mark.parametrize(
    "query,label",
    [
        ({"start_date": "not-a-date", "end_date": "2026-09-17"}, "unparsable"),
        ({"start_date": "2026-09-17", "end_date": "2026-09-10"}, "inverted"),
        ({"start_date": "2026-06-01", "end_date": "2026-09-01"}, "93-days"),
    ],
)
def test_every_unusable_range_is_one_coded_400(client, admin_claim_headers, db, frozen_local_now, senior, query, label):
    """One parser, one code: the tab, the visits list, plan-vs-fact and the exceptions feed
    all branch on SALES_DATE_RANGE_INVALID rather than on four different prose messages."""
    resp = client.get(LIST_URL, headers=admin_claim_headers, query_string=query)
    assert resp.status_code == 400, f"{label}: {resp.get_data(as_text=True)}"
    assert resp.get_json()["error_code"] == "SALES_DATE_RANGE_INVALID", label


def test_the_ceiling_is_ninety_two_days_not_ninety_one(client, admin_claim_headers, db, frozen_local_now, senior):
    """2026-06-02..2026-09-01 is exactly SALES_METRICS_MAX_RANGE_DAYS inclusive days: the
    last range that is allowed, so an off-by-one in the cap fails here and not in Tashkent."""
    resp = client.get(
        LIST_URL,
        headers=admin_claim_headers,
        query_string={"start_date": "2026-06-02", "end_date": "2026-09-01"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert (resp.get_json()["data"]["start_date"], resp.get_json()["data"]["end_date"]) == (
        "2026-06-02",
        "2026-09-01",
    )


def test_a_token_without_the_role_claim_never_reaches_the_service(client, admin_auth_headers, db, senior):
    """`manager_or_higher_required` reads the role CLAIM; the plain admin token carries none."""
    resp = client.get(LIST_URL, headers=admin_auth_headers)
    assert resp.status_code == 403, resp.get_data(as_text=True)
