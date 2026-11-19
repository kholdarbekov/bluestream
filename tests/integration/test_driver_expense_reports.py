"""The cash report carries each driver's approved expenses (spec 2026-11-17 §6.9).

The admin Cash reconciliation report is read through `GET /api/v1/admin/staff/cash-reconciliation`, the route the
admin screen calls with its role-claim token. Trip cash lands through the admin "Record collection" route, claims
through the staff bot's Send and the office's approve route, so every figure is read back through the report.
Figures are the spec's worked example (the driver collected 420,000 so'm, then claims 120,000 for fuel).
"""

import pytest

from tests.integration._driver_expense_helpers import approve, claim
from tests.integration._prepayment_payout_helpers import collect_on_trip

pytestmark = pytest.mark.integration

REPORT = "/api/v1/admin/staff/cash-reconciliation"


def _claim_id(client, app, driver, amount):
    status, answer = claim(client, app, driver, amount)
    assert status == 201, answer
    return answer["data"]["expense"]["id"]


def test_the_report_counts_approved_expenses_per_driver_and_in_total(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver, delivery_driver
):
    driver_a = second_delivery_driver
    driver_b = delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver_a.id, 420000)
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver_b.id, 100000)

    # Driver A: one claim approved in full, one still waiting. Driver B has no claims.
    approved_id = _claim_id(client, app, driver_a, 120000)
    approve_status, approve_body = approve(client, admin_claim_headers, approved_id, 120000)
    assert approve_status == 200, approve_body
    _claim_id(client, app, driver_a, 50000)

    response = client.get(REPORT, headers=admin_claim_headers)
    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]

    assert {r["driver_id"]: r["expenses_approved_total"] for r in body["report"]} == {
        driver_a.id: 120000.0,
        driver_b.id: 0.0,
    }
    assert body["summary"]["expenses_approved_total"] == 120000.0
    row = next(s for s in body["sessions"] if s["driver_user_id"] == driver_a.id)
    assert (row["expenses_approved_total"], row["expenses_waiting_total"]) == (120000.0, 50000.0)
