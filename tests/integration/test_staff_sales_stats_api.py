"""GET /api/v1/staff/sales/me/stats -- the staff bot's My stats card, one window per tap.

Same frozen clock, seeds and helpers as the admin metrics suite (imported, not re-typed):
2026-09-17 08:30 Tashkent, whose `today`, `week` (Mon 09-14..) and `month` (09-01..) are
three different local windows, so a period switch that silently returned the same numbers
three times would fail here.

The gate is the DB staff role, not a JWT claim -- `sales_agent_auth_headers` carries no
`role` claim on purpose -- so an admin token is a 403 on this route even though it is an
admin token.

As in the admin suite, the twenty keys are asserted by MEMBERSHIP: `app.json.sort_keys` is
True, so Flask alphabetises every dict on the wire and the bot's reading order comes from
`METRIC_KEYS` (pinned at the producer), never from the JSON document order.
"""

import pytest

from business_app.services.sales.agent_metrics_service import METRIC_KEYS
from business_app.utils import local_windows
from business_app.utils.local_windows import STATS_PERIODS
from tests.integration.test_admin_sales_metrics_api import (
    AUGUST_23_00,
    FROZEN_LOCAL,
    LAST_TUESDAY_12_00,
    TODAY_00_30,
    TUESDAY_12_00,
    _agent,
    _completed_visit,
    _outlet,
)

pytestmark = pytest.mark.integration

STATS = "/api/v1/staff/sales/me/stats"


@pytest.fixture
def frozen_local_now(monkeypatch):
    """See the admin suite: `local_windows` binds `local_now` by name, so the patch goes
    on that module and nowhere else."""
    monkeypatch.setattr(local_windows, "local_now", lambda: FROZEN_LOCAL)
    return FROZEN_LOCAL


@pytest.fixture
def my_week(db, sales_agent_user):
    """The caller's four visits, plus another agent's day that must never land on the card."""
    outlet = _outlet(db, sales_agent_user, "Bahor market")
    for started_at in (TODAY_00_30, TUESDAY_12_00, LAST_TUESDAY_12_00, AUGUST_23_00):
        _completed_visit(db, sales_agent_user, outlet, started_at)
    neighbour = _agent(db, phone="+998901234574", first_name="Aziz")
    _completed_visit(db, neighbour, _outlet(db, neighbour, "Sunnat do'kon"), TODAY_00_30)
    return sales_agent_user


def test_each_period_is_its_own_local_window(client, sales_agent_auth_headers, db, frozen_local_now, my_week):
    default = client.get(STATS, headers=sales_agent_auth_headers)
    assert default.status_code == 200, default.get_data(as_text=True)
    body = default.get_json()["data"]
    # No `period` means today -- the screen the profile button opens on.
    assert body["period"] == "today"
    assert (body["start_date"], body["end_date"]) == ("2026-09-17", "2026-09-17")
    assert set(body["metrics"]) == set(METRIC_KEYS)
    # Aziz also worked today; his visit is not on this agent's card.
    assert body["metrics"]["completed_visits"] == 1

    week = client.get(STATS, headers=sales_agent_auth_headers, query_string={"period": "week"})
    assert week.status_code == 200, week.get_data(as_text=True)
    week_body = week.get_json()["data"]
    assert (week_body["period"], week_body["start_date"], week_body["end_date"]) == (
        "week",
        "2026-09-14",
        "2026-09-17",
    )
    assert week_body["metrics"]["completed_visits"] == 2

    month = client.get(STATS, headers=sales_agent_auth_headers, query_string={"period": "month"})
    assert month.status_code == 200, month.get_data(as_text=True)
    month_body = month.get_json()["data"]
    assert (month_body["period"], month_body["start_date"], month_body["end_date"]) == (
        "month",
        "2026-09-01",
        "2026-09-17",
    )
    # The 2026-09-08 visit joins; the one at local 2026-08-31 23:00 stays out.
    assert month_body["metrics"]["completed_visits"] == 3


def test_the_period_tuple_is_the_whole_vocabulary(client, sales_agent_auth_headers, db, frozen_local_now, my_week):
    """Three buttons, three periods, one coded 400 for anything else."""
    assert STATS_PERIODS == ("today", "week", "month")
    refused = client.get(STATS, headers=sales_agent_auth_headers, query_string={"period": "quarter"})
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_STATS_PERIOD_INVALID"


def test_the_gate_is_the_db_staff_role_not_a_jwt_claim(client, admin_claim_headers, db, frozen_local_now):
    """An admin token -- claim and all -- is still not a sales agent."""
    refused = client.get(STATS, headers=admin_claim_headers)
    assert refused.status_code == 403, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "STAFF_NO_ROLE"
