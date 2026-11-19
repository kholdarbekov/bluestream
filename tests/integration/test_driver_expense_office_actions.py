"""The office cannot close a trip while a claim waits on it, and the buttons come from the server (spec 2026-11-17).

Verify, resolve and force-close each refuse a trip with a claim still waiting (409 `CASH_SESSION_EXPENSES_PENDING`),
and every session read publishes `can_verify` / `verify_block_code`, `can_resolve` / `resolve_block_code` and
`can_force_close` / `force_close_block_code` from the same rule the routes answer by. The status sets are today's
(owner Q1, 2026-11-18: the office may verify a running trip).

Every test drives the routes the admin cash-reconciliation screen calls, with the bodies it sends: the session list
(`GET .../cash-reconciliation`), the session detail and `POST .../sessions/<id>/verify`, `/resolve` and
`/force-close`. Trip cash lands through the admin "Record collection" route with the driver as collector (or at the
door, `door_cash_on_trip`), claims through the staff bot's Send, hand-ins through the bot's hand-in tap and
decisions through the admin Driver expenses page. Figures are the spec's worked example (the driver collected
420,000 so'm, then claims 150,000 for fuel).
"""

import pytest

from business_app.models.payment import CashCollectionEvent
from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import claim, door_cash_on_trip, options, reject
from tests.integration._prepayment_payout_helpers import (
    BASE,
    SESSION,
    collect_on_trip,
    driver_headers,
    hand_in,
    session_detail,
    stored,
)

pytestmark = pytest.mark.integration

STAFF_COLLECTION = "/api/v1/staff/cash-collections"
REJECT_REASON = "Not a business expense"
SHORT_COUNT = {"verified_cash": 90000, "reason_code": "cash_count_short", "notes": "short"}
BODIES = {
    "verify": SHORT_COUNT,
    "resolve": {"reason_code": "cash_recovered_later", "resolution_notes": "The driver brought the rest"},
    "force-close": {"reason": "Driver left"},
}
# The route's name -> the name its flags carry (`can_<flag>`, `<flag>_block_code`).
FLAG = {"verify": "verify", "resolve": "resolve", "force-close": "force_close"}


def _office(client, headers, session_id, action, body=None):
    """The screen's Verify, Resolve or Force-close, with the body its dialog sends."""
    return client.post(SESSION.format(session_id) + f"/{action}", json=body or BODIES[action], headers=headers)


def _error_code(response):
    return (response.get_json().get("data") or {}).get("error_code")


def _claim_id(client, app, driver, amount):
    status, answer = claim(client, app, driver, amount)
    assert status == 201, answer
    return answer["data"]["expense"]["id"]


def _list_row(client, headers, session_id):
    """The session's row in the cash-reconciliation screen's session list."""
    response = client.get(BASE, headers=headers)
    assert response.status_code == 200, response.get_json()
    (row,) = [row for row in response.get_json()["data"]["sessions"] if row["id"] == session_id]
    return row


def _flags(session, flag):
    return session[f"can_{flag}"], session[f"{flag}_block_code"]


def _set_overdue(db, session_id):
    """Setup only: the overdue sweep marks an open trip overdue once its warning is due; here the row is marked."""
    row = stored(db, session_id)
    row.status = DriverCashSessionStatus.OVERDUE
    db.session.commit()


# --------------------------------------------------------------------------- a waiting claim holds the trip open


def test_force_close_waits_for_the_claim_to_be_decided(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = _claim_id(client, app, driver, 150000)

    r = _office(client, admin_claim_headers, sid, "force-close")

    assert (r.status_code, _error_code(r)) == (409, "CASH_SESSION_EXPENSES_PENDING")
    assert (stored(db, sid).status.value, stored(db, sid).force_close_reason) == ("open", None)

    status, body = reject(client, admin_claim_headers, expense_id, REJECT_REASON)
    assert status == 200, body
    r = _office(client, admin_claim_headers, sid, "force-close")

    assert r.status_code == 200, r.get_json()
    assert (r.get_json()["data"]["status"], stored(db, sid).force_close_reason) == ("force_closed", "Driver left")


def test_verify_waits_for_the_claim_to_be_decided(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    _claim_id(client, app, driver, 150000)
    # Today's verify set takes an open trip; only the waiting claim refuses it.
    assert _flags(session_detail(client, admin_claim_headers, sid), "verify") == (False, "expenses_pending")

    r = _office(client, admin_claim_headers, sid, "verify")

    assert (r.status_code, _error_code(r)) == (409, "CASH_SESSION_EXPENSES_PENDING")
    row = stored(db, sid)
    assert (row.status.value, row.verified_cash, row.blocked_from_cod) == ("open", None, False)


# --------------------------------------------------------------------------- today's status sets, offered by the flags


def test_the_office_verifies_a_running_trip_from_the_screen(
    app, client, db, admin_claim_headers, sample_order, sample_user, second_delivery_driver
):
    """Owner Q1 (2026-11-18): yes. A short count on a running trip ends it as MISMATCH and blocks the driver's cash
    collections until a manager resolves it; his next collection then opens his next trip, which his claim options
    read."""
    driver = second_delivery_driver
    event_id = door_cash_on_trip(db, sample_order, sample_user, driver, total=100000, cash=100000)
    sid = db.session.get(CashCollectionEvent, event_id).driver_cash_session_id
    detail = session_detail(client, admin_claim_headers, sid)
    assert (detail["status"], *_flags(detail, "verify")) == ("open", True, None)

    r = _office(client, admin_claim_headers, sid, "verify", SHORT_COUNT)

    assert r.status_code == 200, r.get_json()
    assert (stored(db, sid).status.value, stored(db, sid).blocked_from_cod) == ("mismatch", True)
    assert options(client, app, driver)["has_active_trip"] is False

    # The mismatch blocks his next collection until a manager resolves the trip.
    collection = {"customer_id": sample_user.id, "amount": 50000, "notes": "Paid at the next door"}
    blocked = client.post(STAFF_COLLECTION, json=collection, headers=driver_headers(app, driver))
    assert (blocked.status_code, blocked.get_json()["error_code"]) == (400, "COD_DRIVER_BLOCKED")
    assert _office(client, admin_claim_headers, sid, "resolve").status_code == 200
    assert (stored(db, sid).status.value, stored(db, sid).blocked_from_cod) == ("resolved", False)

    collected = client.post(STAFF_COLLECTION, json=collection, headers=driver_headers(app, driver))

    assert collected.status_code == 201, collected.get_json()
    next_sid = collected.get_json()["data"]["driver_cash_session"]["id"]
    assert next_sid != sid
    assert stored(db, next_sid).status.value == "open"
    assert options(client, app, driver)["session_id"] == next_sid


def test_resolve_is_offered_on_an_overdue_trip(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    _set_overdue(db, sid)
    detail = session_detail(client, admin_claim_headers, sid)
    assert (detail["status"], *_flags(detail, "resolve")) == ("overdue", True, None)
    assert "resolve_session" in detail["next_actions"]

    r = _office(client, admin_claim_headers, sid, "resolve")

    assert r.status_code == 200, r.get_json()
    assert (r.get_json()["data"]["status"], stored(db, sid).status.value) == ("resolved", "resolved")


# --------------------------------------------------------------------------- Review Focus 5: flag <=> route answer

# Each world's block code per action, from today's status sets (verify: open, partial, submitted, overdue,
# mismatch; resolve: mismatch, overdue; force-close: open, partial, overdue) and the claims rule. A status the set
# refuses answers first. "overdue_with_claim" is the one world where resolve meets a waiting claim.
ACTIONS = ("verify", "resolve", "force-close")
EXPECTED_BLOCK = {
    # world: (verify, resolve, force-close), in ACTIONS order
    "open": (None, "session_status", None),
    "open_with_claim": ("expenses_pending", "session_status", "expenses_pending"),
    "partial": (None, "session_status", None),
    "partial_with_claim": ("expenses_pending", "session_status", "expenses_pending"),
    "submitted": (None, "session_status", "session_status"),
    "mismatch": (None, None, "session_status"),
    "overdue": (None, None, None),
    "overdue_with_claim": ("expenses_pending", "expenses_pending", "expenses_pending"),
}


def _build(world, app, client, db, headers, customer, driver):
    """The trip in ``world``, built fresh through the routes (overdue: the row's status is set in setup)."""
    sid = collect_on_trip(client, headers, customer.id, driver.id, 420000)
    if world.endswith("_with_claim"):
        _claim_id(client, app, driver, 150000)
    if world.startswith("partial"):
        hand_in(client, app, driver, 100000)
    if world in ("submitted", "mismatch"):
        hand_in(client, app, driver, 420000)
    if world == "mismatch":
        assert _office(client, headers, sid, "verify", SHORT_COUNT).status_code == 200
    if world.startswith("overdue"):
        _set_overdue(db, sid)
    assert stored(db, sid).status.value == world.removesuffix("_with_claim")
    return sid


def _answer_for(action, block_code, status):
    """Today's answer for ``block_code``: 200 when nothing blocks, the claims refusal, or each route's own status
    refusal (verify and resolve a 400 naming the status, force-close a 409 with its code)."""
    if block_code is None:
        return 200, None
    if block_code == "expenses_pending":
        return 409, "CASH_SESSION_EXPENSES_PENDING"
    if action == "force-close":
        return 409, "CASH_SESSION_NOT_FORCE_CLOSEABLE"
    return 400, f"Cannot {action} session with current status '{status}'"


def _answer(response):
    body = response.get_json()
    if response.status_code == 200:
        return 200, None
    return response.status_code, _error_code(response) or body["errors"][0]


@pytest.mark.parametrize("world", sorted(EXPECTED_BLOCK))
@pytest.mark.parametrize("action", ACTIONS)
def test_each_published_office_flag_matches_the_routes_answer(
    world, action, app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    sid = _build(world, app, client, db, admin_claim_headers, sample_user, second_delivery_driver)
    expected = EXPECTED_BLOCK[world][ACTIONS.index(action)]
    detail = session_detail(client, admin_claim_headers, sid)
    row = _list_row(client, admin_claim_headers, sid)
    status = detail["status"]

    # The detail and the list row publish the same flags, and next_actions offers resolve exactly when can_resolve.
    assert _flags(detail, FLAG[action]) == _flags(row, FLAG[action]) == (expected is None, expected)
    assert ("resolve_session" in detail["next_actions"]) is detail["can_resolve"]
    assert ("resolve_session" in row["next_actions"]) is row["can_resolve"]

    r = _office(client, admin_claim_headers, sid, action)

    assert _answer(r) == _answer_for(action, expected, status), r.get_json()
    if expected is not None:
        assert stored(db, sid).status.value == status
