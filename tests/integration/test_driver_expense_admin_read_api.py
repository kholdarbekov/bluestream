"""The admin Driver expenses page reads the claims (spec 2026-11-17 §6.8).

Every test drives the two routes the page calls, with the role-claim tokens the admin UI holds:
`GET /api/v1/admin/driver-expenses` (the list with its filters, the badge count and the category totals) and
`GET /api/v1/admin/driver-expenses/<id>` (one claim and its warnings). Trip cash lands through the admin "Record
collection" route and waiting claims through the staff bot's Send; a claim needed in a finished state or at a fixed
time is inserted as setup (`insert_expense`) until the decision routes exist. Figures are the spec's worked example
(the driver collected 420,000 so'm, then claims 150,000 for fuel).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import DeliveryPerson
from business_app.models.payment import DriverCashSession
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import ADMIN, admin_detail, admin_list, claim, insert_expense
from tests.integration._prepayment_payout_helpers import collect_on_trip, driver_headers, hand_in

pytestmark = pytest.mark.integration

PROFILE = "/api/v1/auth/profile"
FLAGS = ("can_approve", "can_reject", "can_undo", "decide_block_code", "approve_block_code", "undo_block_code", "approve_max_amount")
ROW_KEYS = {
    "id",
    "driver_user_id",
    "driver_name",
    "driver_cash_session_id",
    "category",
    "amount",
    "approved_amount",
    "note",
    "has_receipt",
    "latitude",
    "longitude",
    "status",
    "submitted_at",
    "decided_at",
    "decided_by_user_id",
    "decided_by_name",
    "decision_reason",
    "self_decided",
    "withdrawn_at",
    *FLAGS,
}


@pytest.fixture
def operator_claim_headers(app, operator_user):
    with app.app_context():
        token = create_access_token(identity=str(operator_user.id), additional_claims={"role": "operator"})
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _utc(text):
    return datetime.fromisoformat(text)


def _flags(row):
    return {key: row[key] for key in FLAGS}


def _rows_by_id(body):
    return {row["id"]: row for row in body["items"]}


def _claim_id(client, app, driver, amount, **body):
    status, answer = claim(client, app, driver, amount, **body)
    assert status == 201, answer
    return answer["data"]["expense"]["id"]


def _closed_trip(db, driver, status, verified_variance, started_at):
    """Setup only: one finished trip of `driver`, as the office's check left it."""
    session = DriverCashSession(
        session_id=f"closed-{uuid4().hex}",
        driver_user_id=driver.id,
        status=DriverCashSessionStatus(status),
        session_started_at=started_at,
        session_ended_at=started_at + timedelta(hours=8),
        verified_variance=Decimal(verified_variance),
    )
    db.session.add(session)
    db.session.commit()
    return session.id


# --------------------------------------------------------------------------- who reads the page


@pytest.mark.parametrize("who,status", [("admin", 200), ("manager", 200), ("operator", 403), ("driver", 403)])
def test_only_admins_and_managers_read_the_queue(
    request, app, client, db, admin_claim_headers, sample_user, second_delivery_driver, who, status
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = _claim_id(client, app, driver, 150000)
    headers = driver_headers(app, driver) if who == "driver" else request.getfixturevalue(f"{who}_claim_headers")
    page = app.test_client()

    listed = page.get(ADMIN, headers=headers)
    opened = page.get(f"{ADMIN}/{expense_id}", headers=headers)

    assert (listed.status_code, opened.status_code) == (status, status), (listed.get_json(), opened.get_json())
    if status == 200:
        assert [row["id"] for row in listed.get_json()["data"]["items"]] == [expense_id]
        assert opened.get_json()["data"]["expense"]["id"] == expense_id
    else:
        assert "data" not in listed.get_json() and "data" not in opened.get_json()


def test_the_profile_publishes_the_flag_to_admins_and_managers(
    app, db, admin_claim_headers, manager_claim_headers, operator_claim_headers, second_delivery_driver
):
    headers = (
        admin_claim_headers,
        manager_claim_headers,
        operator_claim_headers,
        driver_headers(app, second_delivery_driver),
    )

    flags = [
        app.test_client().get(PROFILE, headers=h).get_json()["data"]["permissions"]["can_review_driver_expenses"]
        for h in headers
    ]

    assert flags == [True, True, False, False]


# --------------------------------------------------------------------------- the row and its flags


def test_a_waiting_row_publishes_its_flags(app, client, db, admin_claim_headers, sample_user, second_delivery_driver):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = _claim_id(client, app, driver, 150000, note="Full tank")

    body = admin_list(client, admin_claim_headers)

    assert len(body["items"]) == 1
    row = body["items"][0]
    assert set(row) == ROW_KEYS
    assert _flags(row) == {
        "can_approve": True,
        "can_reject": True,
        "can_undo": False,
        "decide_block_code": None,
        "approve_block_code": None,
        "undo_block_code": "not_approved",
        "approve_max_amount": 150000.0,
    }
    assert (row["id"], row["driver_user_id"], row["driver_name"], row["driver_cash_session_id"]) == (
        expense_id,
        driver.id,
        "Second Driver",
        sid,
    )
    assert (row["category"], row["amount"], row["approved_amount"], row["note"], row["has_receipt"]) == (
        "fuel",
        150000.0,
        None,
        "Full tank",
        False,
    )
    assert (row["status"], row["latitude"], row["longitude"], row["self_decided"]) == ("pending", None, None, False)
    assert (row["decided_at"], row["decided_by_user_id"], row["decided_by_name"]) == (None, None, None)
    assert (row["decision_reason"], row["withdrawn_at"]) == (None, None)
    assert _utc(row["submitted_at"]).tzinfo is not None
    # The dialog reads the same row the list drew.
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"] == row


def test_two_waiting_claims_publish_their_own_approve_max(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    """Review Focus 1: each claim's maximum adds back only its own amount to "Hand in now"."""
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    a = _claim_id(client, app, driver, 150000)
    b = _claim_id(client, app, driver, 50000)
    hand_in(client, app, driver, 220000)  # his "Hand in now": 420,000 − 200,000 waiting

    rows = _rows_by_id(admin_list(client, admin_claim_headers))

    assert (rows[a]["approve_max_amount"], rows[b]["approve_max_amount"]) == (150000.0, 50000.0)
    assert (rows[a]["can_approve"], rows[b]["can_approve"]) == (True, True)
    assert [admin_detail(client, admin_claim_headers, eid)["expense"] for eid in (a, b)] == [rows[a], rows[b]]


def test_a_claim_the_trip_cash_cannot_cover_publishes_why_approve_is_off(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    """Approve is off when the approve maximum is 0; the row then names the reason, so the page derives nothing."""
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    insert_expense(db, sid, driver.id, 500000)  # claims waiting beyond the 420,000 collected: "Hand in now" -180,000
    expense_id = insert_expense(db, sid, driver.id, 100000)

    row = _rows_by_id(admin_list(client, admin_claim_headers))[expense_id]

    assert (row["approve_max_amount"], row["can_approve"], row["can_reject"]) == (0.0, False, True)
    assert (row["approve_block_code"], row["decide_block_code"]) == ("amount_above_trip_cash", None)
    assert row["can_approve"] == (row["approve_block_code"] is None)
    assert admin_detail(client, admin_claim_headers, expense_id)["expense"] == row


def test_a_page_of_rows_reads_the_claims_waiting_on_its_trips_once(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, second_delivery_driver, monkeypatch
):
    """Review Focus 5: the list batches the waiting totals; a per-row read could publish figures the detail does not."""
    sids = [
        collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
        for driver in (delivery_driver, second_delivery_driver)
    ]
    for driver in (delivery_driver, second_delivery_driver):
        _claim_id(client, app, driver, 150000)
        _claim_id(client, app, driver, 50000)
    batched, per_row = [], []
    batch = DriverReconciliationService.expense_totals_for_sessions
    single = DriverReconciliationService._sum_waiting_expenses
    monkeypatch.setattr(
        DriverReconciliationService,
        "expense_totals_for_sessions",
        lambda self, session_ids: batched.append(sorted(session_ids)) or batch(self, session_ids),
    )
    monkeypatch.setattr(
        DriverReconciliationService,
        "_sum_waiting_expenses",
        lambda self, session: per_row.append(session.id) or single(self, session),
    )

    rows = admin_list(client, admin_claim_headers)["items"]

    assert (len(rows), batched, per_row) == (4, [sorted(sids)], [])
    assert {row["approve_max_amount"] for row in rows} == {150000.0, 50000.0}


def test_a_manager_who_also_drives_cannot_decide_his_own_claim(
    app,
    client,
    db,
    admin_user,
    admin_claim_headers,
    manager_user,
    manager_claim_headers,
    sample_user,
    second_delivery_driver,
):
    manager_user.staff_roles = ["delivery_driver"]
    db.session.add(
        DeliveryPerson(
            user_id=manager_user.id,
            full_name="Malika Menejer",
            phone=manager_user.phone,
            is_active=True,
            is_available=True,
        )
    )
    db.session.commit()
    manager_sid = collect_on_trip(client, admin_claim_headers, sample_user.id, manager_user.id, 420000)
    mine_id = _claim_id(client, app, manager_user, 150000)
    approved_id = insert_expense(db, manager_sid, manager_user.id, 20000, status="approved", decided_by=admin_user.id)
    collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    theirs_id = _claim_id(client, app, second_delivery_driver, 150000)

    manager_rows = _rows_by_id(admin_list(client, manager_claim_headers, status="all"))
    admin_rows = _rows_by_id(admin_list(client, admin_claim_headers, status="all"))

    mine, admin_view = manager_rows[mine_id], admin_rows[mine_id]
    assert (mine["can_approve"], mine["can_reject"], mine["decide_block_code"]) == (False, False, "self_decision")
    assert admin_view["can_approve"] is True and admin_view["decide_block_code"] is None
    assert (manager_rows[theirs_id]["can_approve"], manager_rows[theirs_id]["decide_block_code"]) == (True, None)
    # Undoing his own approval is refused to him too, and offered to an admin.
    assert (manager_rows[approved_id]["can_undo"], manager_rows[approved_id]["undo_block_code"]) == (
        False,
        "self_decision",
    )
    assert (admin_rows[approved_id]["can_undo"], admin_rows[approved_id]["undo_block_code"]) == (True, None)
    assert admin_detail(client, manager_claim_headers, mine_id)["expense"] == mine


# --------------------------------------------------------------------------- the period, totals and warnings


def test_the_period_totals_and_duplicate_warning_use_the_tashkent_day(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    """Review Focus 3: a claim at 00:30 Tashkent belongs to that local day, though UTC still reads the one before."""
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    approved = {"status": "approved", "decided_by": admin_user.id}
    insert_expense(db, sid, driver.id, 50000, submitted_at=_utc("2026-11-17T19:30:00+00:00"), **approved)  # 00:30, 18th
    insert_expense(db, sid, driver.id, 70000, submitted_at=_utc("2026-11-17T18:30:00+00:00"), **approved)  # 23:30, 17th

    on_18th = admin_list(client, admin_claim_headers, status="all", start_date="2026-11-18", end_date="2026-11-18")
    on_17th = admin_list(client, admin_claim_headers, status="all", start_date="2026-11-17", end_date="2026-11-17")

    assert [r["amount"] for r in on_18th["items"]] == [50000.0]
    assert {t["category"]: t["approved_total"] for t in on_18th["totals_by_category"]}["fuel"] == 50000.0
    assert [r["amount"] for r in on_17th["items"]] == [70000.0]
    assert {t["category"]: t["approved_total"] for t in on_17th["totals_by_category"]}["fuel"] == 70000.0

    first = insert_expense(db, sid, driver.id, 30000, submitted_at=_utc("2026-11-17T19:30:00+00:00"))  # 00:30, 18th
    second = insert_expense(db, sid, driver.id, 30000, submitted_at=_utc("2026-11-18T18:00:00+00:00"))  # 23:00, 18th
    third = insert_expense(db, sid, driver.id, 30000, submitted_at=_utc("2026-11-18T19:30:00+00:00"))  # 00:30, 19th
    # Taken back the same day: no longer a possible duplicate.
    insert_expense(db, sid, driver.id, 30000, status="withdrawn", submitted_at=_utc("2026-11-18T10:00:00+00:00"))

    duplicates = {
        eid: admin_detail(client, admin_claim_headers, eid)["warnings"]["possible_duplicates"]
        for eid in (first, second, third)
    }

    assert duplicates == {
        first: [{"id": second, "amount": 30000.0, "status": "pending", "submitted_at": "2026-11-18T18:00:00+00:00"}],
        second: [{"id": first, "amount": 30000.0, "status": "pending", "submitted_at": "2026-11-17T19:30:00+00:00"}],
        third: [],
    }


def test_the_totals_add_what_was_approved_per_category_in_tuple_order(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    decided = {"decided_by": admin_user.id}
    insert_expense(db, sid, driver.id, 150000, status="approved", approved_amount=120000, **decided)
    insert_expense(db, sid, driver.id, 30000, status="approved", category="caps", **decided)
    insert_expense(db, sid, driver.id, 20000, status="rejected", category="labels", **decided)
    _claim_id(client, app, driver, 50000, category="vehicle")

    totals = admin_list(client, admin_claim_headers)["totals_by_category"]

    assert totals == [
        {"category": "fuel", "approved_total": 120000.0},
        {"category": "new_bottles", "approved_total": 0.0},
        {"category": "caps", "approved_total": 30000.0},
        {"category": "labels", "approved_total": 0.0},
        {"category": "other_supplies", "approved_total": 0.0},
        {"category": "vehicle", "approved_total": 0.0},
        {"category": "parking_wash", "approved_total": 0.0},
        {"category": "other", "approved_total": 0.0},
    ]


def test_the_badge_count_is_the_number_waiting(
    app, client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    _claim_id(client, app, driver, 100000)
    _claim_id(client, app, driver, 50000)
    insert_expense(db, sid, driver.id, 20000, status="approved", category="caps", decided_by=admin_user.id)
    insert_expense(db, sid, driver.id, 10000, status="rejected", decided_by=admin_user.id)
    insert_expense(db, sid, driver.id, 5000, status="withdrawn")

    answers = {
        status: admin_list(client, admin_claim_headers, status=status)
        for status in ("pending", "approved", "rejected", "withdrawn", "all")
    }
    filtered = [
        admin_list(client, admin_claim_headers),
        admin_list(client, admin_claim_headers, driver_id=driver.id),
        admin_list(client, admin_claim_headers, driver_id=admin_user.id),
        admin_list(client, admin_claim_headers, status="all", category="caps"),
    ]

    assert {status: (body["pending_count"], body["total"]) for status, body in answers.items()} == {
        "pending": (2, 2),
        "approved": (2, 1),
        "rejected": (2, 1),
        "withdrawn": (2, 1),
        "all": (2, 5),
    }
    assert [row["status"] for row in answers["approved"]["items"]] == ["approved"]
    assert [(body["pending_count"], body["total"]) for body in filtered] == [(2, 2), (2, 2), (2, 0), (2, 1)]
    assert [row["amount"] for row in filtered[3]["items"]] == [20000.0]


def test_the_list_and_detail_publish_the_reason_minimum(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = _claim_id(client, app, driver, 150000)

    assert (
        admin_list(client, admin_claim_headers)["reason_min_length"]
        == 5
        == admin_detail(client, admin_claim_headers, expense_id)["reason_min_length"]
    )


def test_the_detail_lists_the_drivers_last_two_closed_trips_newest_first(
    app, client, db, admin_claim_headers, sample_user, delivery_driver, second_delivery_driver
):
    driver = second_delivery_driver
    _closed_trip(db, driver, "verified", "0.00", datetime(2026, 11, 10, 4, tzinfo=UTC))
    mismatch = _closed_trip(db, driver, "mismatch", "-20000.00", datetime(2026, 11, 12, 4, tzinfo=UTC))
    resolved = _closed_trip(db, driver, "resolved", "-5000.00", datetime(2026, 11, 14, 4, tzinfo=UTC))
    _closed_trip(db, delivery_driver, "verified", "0.00", datetime(2026, 11, 15, 4, tzinfo=UTC))  # another driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)
    expense_id = _claim_id(client, app, driver, 150000)

    warnings = admin_detail(client, admin_claim_headers, expense_id)["warnings"]

    assert warnings == {
        "possible_duplicates": [],
        "recent_closed_trips": [
            {
                "session_id": resolved,
                "status": "resolved",
                "verified_variance": -5000.0,
                "session_ended_at": "2026-11-14T12:00:00+00:00",
            },
            {
                "session_id": mismatch,
                "status": "mismatch",
                "verified_variance": -20000.0,
                "session_ended_at": "2026-11-12T12:00:00+00:00",
            },
        ],
    }


def test_an_unknown_claim_is_not_found(app, db, admin_claim_headers):
    response = app.test_client().get(f"{ADMIN}/999999", headers=admin_claim_headers)

    assert (response.status_code, response.get_json()["error_code"]) == (404, "DRIVER_EXPENSE_NOT_FOUND")
