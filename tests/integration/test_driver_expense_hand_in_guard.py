"""The hand-in respects the claims waiting on the trip (spec 2026-11-17 §6.4).

Every test posts the body the staff bot sends to `POST /api/v1/staff/reconciliation/session/submit`: the Cash
screen's button (`declared_cash`, `session_id`, `basis` = the waiting total it was drawn with), a typed amount
(`declared_cash` only) or "settle everything" (`{}`). Trip cash lands through the admin "Record collection" route
with the driver as collector and the claim through the staff bot's claim route. Expected figures are the spec's
worked example: the driver collected 420,000 so'm and claims 150,000 for fuel, so "Hand in now" is 270,000.
"""

import pytest

from business_app.models.payment import DriverCashHandoff
from tests.integration._driver_expense_helpers import claim, hand_in_body
from tests.integration._prepayment_payout_helpers import collect_on_trip

pytestmark = pytest.mark.integration


@pytest.fixture
def trip_with_claim(app, client, db, admin_claim_headers, sample_user, second_delivery_driver):
    """420,000 collected on the driver's trip and a 150,000 fuel claim waiting on it. Returns the trip's id."""
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    status, body = claim(client, app, second_delivery_driver, 150000)
    assert status == 201, body
    return sid


def _handoffs(session_id):
    return [
        float(h.amount)
        for h in DriverCashHandoff.query.filter_by(driver_cash_session_id=session_id).order_by(DriverCashHandoff.id)
    ]


# --------------------------------------------------------------------------- the button


def test_handing_in_exactly_hand_in_now_keeps_the_claim_on_the_trip(
    app, client, second_delivery_driver, trip_with_claim
):
    sid = trip_with_claim

    r = hand_in_body(client, app, second_delivery_driver, {"declared_cash": 270000, "session_id": sid, "basis": 150000})

    assert (r.status_code, r.get_json()["data"]["status"], r.get_json()["data"]["next_actions"]) == (
        200,
        "partial",
        ["await_expense_decision"],
    )
    data = r.get_json()["data"]
    assert (data["id"], data["next_active_session"], data["remaining_cash_to_submit"]) == (sid, None, 0.0)
    assert _handoffs(sid) == [270000.0]


def test_a_button_drawn_before_the_claim_redraws(app, client, second_delivery_driver, trip_with_claim):
    r = hand_in_body(
        client, app, second_delivery_driver, {"declared_cash": 420000, "session_id": trip_with_claim, "basis": 0}
    )

    assert (r.status_code, r.get_json()["error_code"], r.get_json()["details"]) == (
        409,
        "STATE_CHANGED",
        {"reason": "hand_in_changed"},
    )
    assert DriverCashHandoff.query.count() == 0


def test_a_button_whose_amount_no_longer_fits_redraws(app, client, second_delivery_driver, trip_with_claim):
    """The waiting total is the one the button was drawn with, but a typed hand-in since lowered "Hand in now"."""
    sid = trip_with_claim
    assert hand_in_body(client, app, second_delivery_driver, {"declared_cash": 100000}).status_code == 200

    r = hand_in_body(client, app, second_delivery_driver, {"declared_cash": 270000, "session_id": sid, "basis": 150000})

    assert (r.status_code, r.get_json()["error_code"], r.get_json()["details"]) == (
        409,
        "STATE_CHANGED",
        {"reason": "hand_in_changed"},
    )
    assert _handoffs(sid) == [100000.0]


def test_a_button_with_an_unreadable_basis_redraws(app, client, second_delivery_driver, trip_with_claim):
    sid = trip_with_claim

    r = hand_in_body(
        client, app, second_delivery_driver, {"declared_cash": 270000, "session_id": sid, "basis": "150000.001"}
    )

    assert (r.status_code, r.get_json()["error_code"], r.get_json()["details"]) == (
        409,
        "STATE_CHANGED",
        {"reason": "hand_in_changed"},
    )
    assert _handoffs(sid) == []


def test_a_button_from_a_closed_trip_never_posts_onto_the_next(
    app, client, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    t1 = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    first = hand_in_body(client, app, driver, {"declared_cash": 420000, "session_id": t1, "basis": 0})
    assert (first.status_code, first.get_json()["data"]["status"]) == (200, "submitted"), first.get_json()
    t2 = first.get_json()["data"]["next_active_session"]["id"]
    assert collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 50000) == t2

    r = hand_in_body(client, app, driver, {"declared_cash": 420000, "session_id": t1, "basis": 0})

    assert (r.status_code, r.get_json()["error_code"]) == (409, "RECONCILIATION_SESSION_CHANGED")
    assert (_handoffs(t1), _handoffs(t2)) == ([420000.0], [])


# --------------------------------------------------------------------------- a typed amount


def test_a_typed_hand_in_above_hand_in_now_is_refused_while_a_claim_waits(
    app, client, second_delivery_driver, trip_with_claim
):
    r = hand_in_body(client, app, second_delivery_driver, {"declared_cash": 270000.01})

    assert (r.status_code, r.get_json()["error_code"], r.get_json()["details"]["hand_in_now"]) == (
        409,
        "RECONCILIATION_EXPENSES_WAITING",
        270000.0,
    )
    assert r.get_json()["details"] == {"hand_in_now": 270000.0, "expenses_waiting_total": 150000.0}
    assert DriverCashHandoff.query.count() == 0


def test_without_a_claim_over_handing_is_still_allowed(
    app, client, admin_claim_headers, sample_user, second_delivery_driver
):
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)

    r = hand_in_body(client, app, second_delivery_driver, {"declared_cash": 500000})

    data = r.get_json()["data"]
    assert (r.status_code, data["id"], data["status"], data["declared_variance"]) == (200, sid, "submitted", 80000.0)
    assert _handoffs(sid) == [500000.0]


# --------------------------------------------------------------------------- settle everything


def test_settle_everything_hands_in_the_net_figure_and_then_nothing(
    app, client, second_delivery_driver, trip_with_claim
):
    sid = trip_with_claim

    first = hand_in_body(client, app, second_delivery_driver, {})

    data = first.get_json()["data"]
    assert (first.status_code, data["status"], data["next_actions"]) == (200, "partial", ["await_expense_decision"])
    assert _handoffs(sid) == [270000.0]

    again = hand_in_body(client, app, second_delivery_driver, {})

    assert (again.status_code, again.get_json()["error_code"]) == (400, "RECONCILIATION_NOTHING_TO_SUBMIT")
    assert _handoffs(sid) == [270000.0]
