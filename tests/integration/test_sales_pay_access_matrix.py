"""Who may see pay (compensation spec §5.6, §9 S-15; §10.3 T-VISIB-1 and T-VISIB-3).

- Every admin pay route (A0-A26) is `super_admin_required`. The cases are generated from
  `app.url_map` under /api/v1/admin/sales/pay/, so a pay route added later is covered without
  editing this file.
- The admin UI shows Compensation from `permissions.can_manage_sales_pay` (never from the role):
  the flag and the decorator state one rule, and the tie test checks it in both directions over
  every route.
- The agent routes (S1, S2) answer for the token's own agent: there is no agent id to pass, and
  one passed anyway is ignored. Only the `sales_agent` staff role reaches them.

Every admin call below sends an empty body with ids = 1 and month 2026-10 on an estate where pay
has not started, so no call can write anything: the worst an admin meets is a 400, 404 or 409.
"""

import re
from datetime import date

import pytest

from business_app.services.auth_service import AuthService
from tests.integration.sales_pay_builders import (
    claim_headers,
    configure_agent,
    local_at,
    move_clock,
    pay_agent,
    pay_call,
    pay_world,
    refusal,
    water_order,
)
from tests.integration.test_sales_pay_agent_earnings_api import EARNINGS, LINES, lapse_throttle

pytestmark = pytest.mark.integration

PAY_PREFIX = "/api/v1/admin/sales/pay/"
_PLACEHOLDER = re.compile(r"<(?:(\w+):)?(\w+)>")


def pay_route_cases(app):
    """(method, path) of every admin pay route: `<int:…>` becomes 1, the `<month>` segment 2026-10."""
    cases = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith(PAY_PREFIX):
            continue
        path = _PLACEHOLDER.sub(lambda match: "1" if match.group(1) == "int" else "2026-10", rule.rule)
        cases.update((method, path) for method in rule.methods - {"HEAD", "OPTIONS"})
    return sorted(cases)


def call(client, method: str, path: str, headers):
    kwargs = {"headers": headers}
    if method != "GET":
        kwargs["json"] = {}
    return client.open(path, method=method, **kwargs)


def test_every_admin_pay_route_refuses_a_manager_and_admits_an_admin(
    app, client, db, admin_claim_headers, manager_claim_headers
):
    """T-VISIB-1, generated over the URL map: a manager's claim is refused (403) on every
    (method, rule) pair under /api/v1/admin/sales/pay/, and an admin's never is."""
    cases = pay_route_cases(app)
    assert len(cases) >= 27  # A0-A26 today; a later pay route only adds cases

    for method, path in cases:
        as_manager = call(client, method, path, manager_claim_headers)
        assert as_manager.status_code == 403, (method, path, as_manager.get_data(as_text=True))
        as_admin = call(client, method, path, admin_claim_headers)
        assert as_admin.status_code != 403 and as_admin.status_code < 500, (
            method, path, as_admin.status_code, as_admin.get_data(as_text=True),
        )


def test_the_pay_flag_and_the_pay_routes_state_one_rule(
    app, client, db, admin_user, manager_user, admin_claim_headers, manager_claim_headers
):
    """§5.6's tie test, both directions: the user whose `can_manage_sales_pay` is false is refused on
    every pay route, and the user whose flag is true is refused on none. A route guarded by
    anything else, or a flag computed from anything else, splits the two sets."""
    cases = pay_route_cases(app)
    flags = {}
    for user, headers in ((admin_user, admin_claim_headers), (manager_user, manager_claim_headers)):
        flag = AuthService().get_user_permissions(user.id)["can_manage_sales_pay"]
        refused = {(method, path) for method, path in cases if call(client, method, path, headers).status_code == 403}
        assert refused == (set() if flag else set(cases)), (user.role, flag, sorted(refused))
        flags[user.id] = flag

    assert flags == {admin_user.id: True, manager_user.id: False}


def test_an_agent_reads_only_their_own_pay(
    app, client, db, monkeypatch, admin_user, admin_claim_headers, manager_claim_headers, sales_agent_user,
    sales_agent_auth_headers, driver_auth_headers,
):
    """T-VISIB-3. Agent A has a 9,000 credit in October (6 x 19 L at 1,500); agent B has none. B asks
    S1 and S2 for A's pay with `agent_id=<A>`: the parameter is ignored and B gets B's own. A driver
    is not a sales agent (403 STAFF_NO_ROLE), and neither is an admin or a manager, whatever their
    token claims."""
    world = pay_world(db, client, monkeypatch, admin_user, admin_claim_headers, sales_agent_user)
    other = pay_agent(db, phone="+998901238702", first_name="Bekzod")
    configure_agent(client, admin_claim_headers, other, plan_id=world.plan["id"], base=3000000,
                    effective_month="2026-10", employment_start="2026-10-01")
    water_order(world, 6, date(2026, 10, 5))
    move_clock(world, local_at(2026, 10, 12, 10))
    other_headers = claim_headers(app, other, "sales_agent")
    lapse_throttle(sales_agent_user)
    lapse_throttle(other)

    mine = pay_call(client, "GET", EARNINGS, sales_agent_auth_headers)
    asked_for_mine = pay_call(client, "GET", f"{EARNINGS}?agent_id={sales_agent_user.id}", other_headers)
    my_lines = pay_call(client, "GET", f"{LINES}?month=2026-10", sales_agent_auth_headers)
    lines_asked_for_mine = pay_call(client, "GET", f"{LINES}?month=2026-10&agent_id={sales_agent_user.id}", other_headers)

    assert mine["open_months"][0]["estimate"]["commission"]["gross"] == 9000.0
    assert asked_for_mine["open_months"][0]["estimate"]["commission"]["gross"] == 0.0
    assert asked_for_mine == pay_call(client, "GET", EARNINGS, other_headers)
    assert [line["amount"] for line in my_lines["items"]] == [9000.0]
    assert (lines_asked_for_mine["available"], lines_asked_for_mine["items"]) == (True, [])

    for path in (EARNINGS, f"{LINES}?month=2026-10"):
        refusal(client, "GET", path, driver_auth_headers, status=403, code="STAFF_NO_ROLE")
        refusal(client, "GET", path, admin_claim_headers, status=403, code="STAFF_NO_ROLE")
        assert client.get(path, headers=manager_claim_headers).status_code == 403
