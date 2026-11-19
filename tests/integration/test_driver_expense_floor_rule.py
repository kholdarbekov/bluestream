"""Owner Q2 (2026-11-18): a collected-cash correction on a trip with approved expenses is allowed, as for any trip.

The expense floor exists only while a claim waits on the trip (`expense_floor` is None otherwise), so once the claim
is approved a correction goes through as it does today: on a closed trip, and on a verified trip it reopens. Approved
expenses stay out of the trip's on hand across the correction and the reopen.

Every test drives the routes the admin screens call: the order detail, the collected-cash preview and apply, the cash
reconciliation session detail, verify and force-close. Door cash lands through `door_cash_on_trip`, the claim through
the staff bot's Send, the hand-in through the bot's hand-in tap and the approval through the admin Driver expenses
page. Figures: a 100,000 COD order paid 100,000 in cash, a 30,000 claim approved in full and 70,000 handed in.
"""

import inspect

import pytest

from business_app.models.payment import CashCollectionEvent
from business_app.services.notification_service import NotificationService
from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import approve, claim, door_cash_on_trip, options
from tests.integration._prepayment_payout_helpers import SESSION, hand_in, session_detail, stored

pytestmark = pytest.mark.integration


def _order_detail(client, headers, order_id):
    """The admin order detail the "Edit collected cash" dialog reads its flags from."""
    response = client.get(f"/api/v1/admin/orders/{order_id}", headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["order"]


def _preview(client, headers, order_id, new_amount):
    """The dialog's preview, asked whenever the amount changes."""
    response = client.post(
        f"/api/v1/admin/orders/{order_id}/collected-cash/preview", json={"new_amount": new_amount}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _apply(client, headers, order_id, new_amount):
    """The dialog's Save, with the reason typed. Returns the response without asserting."""
    return client.post(
        f"/api/v1/admin/orders/{order_id}/collected-cash",
        json={"new_amount": new_amount, "reason": "Driver miscounted"},
        headers=headers,
    )


@pytest.fixture
def staff_messages(monkeypatch):
    """The staff bot Telegram messages the backend sends (the reopen notice), bound like the real call."""
    signature = inspect.signature(NotificationService.send_staff_telegram_message)
    sent = []

    def _record(self, user, message, **kwargs):
        signature.bind(self, user, message, **kwargs)  # TypeError on a call the real method refuses
        sent.append((user.id, message))
        return {"success": True}

    monkeypatch.setattr(NotificationService, "send_staff_telegram_message", _record)
    return sent


@pytest.fixture
def approved_trip(app, client, db, admin_claim_headers, sample_order, sample_user, second_delivery_driver):
    """100,000 collected at the door, a 30,000 claim, the 70,000 the Cash screen asked for handed in, then the claim
    approved in full: on hand 70,000 equals what was handed in, so the approval closed the trip and opened the
    driver's next, empty one. Returns the order and the closed trip's id."""
    driver = second_delivery_driver
    event_id = door_cash_on_trip(db, sample_order, sample_user, driver, total=100000, cash=100000)
    session_id = db.session.get(CashCollectionEvent, event_id).driver_cash_session_id
    status, body = claim(client, app, driver, 30000)
    assert status == 201, body
    assert hand_in(client, app, driver, 70000)["status"] == "partial"
    status, decided = approve(client, admin_claim_headers, body["data"]["expense"]["id"], 30000)
    assert status == 200, decided
    assert stored(db, session_id).status == DriverCashSessionStatus.SUBMITTED
    return sample_order, session_id


def test_a_correction_on_a_closed_trip_with_only_approved_expenses_is_allowed(
    client, db, admin_claim_headers, approved_trip
):
    order, sid = approved_trip
    assert _order_detail(client, admin_claim_headers, order.id)["collected_cash_edit_min_amount"] is None

    r = _apply(client, admin_claim_headers, order.id, 90000)

    assert r.status_code == 200, r.get_json()
    detail = session_detail(client, admin_claim_headers, sid)
    # On hand 90,000 - 30,000 approved = 60,000 against 70,000 handed in: he handed in more than is now recorded.
    assert (detail["status"], detail["expected_cash_on_hand"], detail["declared_variance"]) == (
        "submitted",
        60000.0,
        10000.0,
    )
    assert _order_detail(client, admin_claim_headers, order.id)["collected_cash_edit_min_amount"] is None


def test_a_correction_that_reopens_a_verified_trip_with_an_approved_expense_is_allowed(
    app, client, db, admin_claim_headers, second_delivery_driver, approved_trip, staff_messages
):
    order, sid = approved_trip
    verified = client.post(
        SESSION.format(sid) + "/verify",
        json={"verified_cash": 70000, "reason_code": "cash_count_matched"},
        headers=admin_claim_headers,
    )
    assert (verified.status_code, verified.get_json()["data"]["status"]) == (200, "verified")
    # The driver's next trip is still empty and open; closed, the reopen has nothing to collide with.
    next_sid = options(client, app, second_delivery_driver)["session_id"]
    assert next_sid not in (None, sid)
    closed = client.post(
        SESSION.format(next_sid) + "/force-close", json={"reason": "Driver off this week"}, headers=admin_claim_headers
    )
    assert (closed.status_code, closed.get_json()["data"]["status"]) == (200, "force_closed")
    preview = _preview(client, admin_claim_headers, order.id, 90000)
    assert (preview["session_will_reopen"], preview["blocking_reasons"]) == (True, [])

    r = _apply(client, admin_claim_headers, order.id, 90000)

    assert r.status_code == 200, r.get_json()
    detail = session_detail(client, admin_claim_headers, sid)
    # Reopened: the handoffs are voided for a fresh hand-in, and on hand stays net of the approved 30,000.
    assert (detail["status"], detail["expected_cash_on_hand"], detail["expenses_approved_total"]) == (
        "open",
        60000.0,
        30000.0,
    )
    assert staff_messages == [
        (
            second_delivery_driver.id,
            f"Your cash session #{sid} was reopened by admin because the collected cash for order #{order.id} was "
            "corrected. Please re-submit when ready.",
        )
    ]
