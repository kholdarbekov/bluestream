"""A collected-cash correction that must reopen a verified trip is refused while the driver's current trip
is still partial (the unique index counts open, overdue and partial trips)."""

import pytest

from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import door_cash_on_trip
from tests.integration._prepayment_payout_helpers import SESSION, collect_on_trip, hand_in, stored

pytestmark = pytest.mark.integration


def test_a_correction_on_a_verified_trip_while_the_current_trip_is_partial_is_409_not_500(
    app, client, db, admin_claim_headers, sample_order, sample_user, second_delivery_driver
):
    door_cash_on_trip(db, sample_order, sample_user, second_delivery_driver, total=100000, cash=100000)
    t1 = hand_in(client, app, second_delivery_driver, 100000)["id"]  # T1 submitted, T2 opens
    r = client.post(
        SESSION.format(t1) + "/verify",
        json={"verified_cash": 100000, "reason_code": "cash_count_matched"},
        headers=admin_claim_headers,
    )
    assert r.get_json()["data"]["status"] == "verified"
    collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 50000)
    hand_in(client, app, second_delivery_driver, 20000)  # T2 partial
    preview = client.post(
        f"/api/v1/admin/orders/{sample_order.id}/collected-cash/preview",
        json={"new_amount": 90000},
        headers=admin_claim_headers,
    ).get_json()["data"]
    assert any(r.startswith("cash_session_active_conflict: ") for r in preview["blocking_reasons"])
    r = client.post(
        f"/api/v1/admin/orders/{sample_order.id}/collected-cash",
        json={"new_amount": 90000, "reason": "Miscounted"},
        headers=admin_claim_headers,
    )
    assert (r.status_code, r.get_json()["data"]["error_code"]) == (409, "CASH_SESSION_ACTIVE_CONFLICT")
    assert stored(db, t1).status == DriverCashSessionStatus.VERIFIED
