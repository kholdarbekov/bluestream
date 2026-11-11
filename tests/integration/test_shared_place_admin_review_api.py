"""The admin's review of shared-place cases (spec 2026-11-10 §4.5, §4.7; §9 binds): the list, one case with its live
facts, and what the case screen offers.

Driven through the two admin GET routes the Driver reports tab calls. Cases open through the real delivery routes
(the staff bot's Delivered tap) wherever detection can build them; a case whose frozen candidates must point at
something detection cannot find (an address that has no empties, a group about to be dissolved) is written as
detection writes it. Linking and grouping go through the real admin link and place-group endpoints.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from flask import current_app
from flask_jwt_extended import create_access_token

from business_app.models.shared_place_case import SharedPlaceCase
from business_app.models.user import UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.tryout_service import TryoutService
from business_app.utils.timezone_utils import ensure_utc, get_utc_now
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    empties_at,
    pinned_address,
    stop_for,
)
from tests.integration.test_shared_place_detection import _driver_delivers
from tests.unit.test_tryout_service import _build_payload

pytestmark = pytest.mark.integration

LIST = "/api/v1/admin/shared-place-cases"
DETAIL = "/api/v1/admin/shared-place-cases/{}"
PLACE_GROUPS = "/api/v1/admin/place-groups"
GROUP_MEMBER = "/api/v1/admin/place-groups/{}/addresses/{}"
LINK = "/api/v1/admin/users/{}/link"
ANSWER = "/api/v1/staff/shared-place-cases/{}/answer"
M14 = 0.000126  # ~14 m of latitude
FAR = 0.01  # ~1.1 km: never a candidate
ALL_KINDS = ["shared_place", "same_home", "other_place", "own_bottles", "wrong_count", "close"]
GONE = {"exists": False, "place_group_id": None, "label": None, "empties": 0, "members": []}


# --------------------------------------------------------------------------------------------------------------------
# Helpers: every state change goes through the endpoint the admin UI or the staff bot calls
# --------------------------------------------------------------------------------------------------------------------


def _bearer(user):
    token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(url, headers):
    """A fresh client per call, so the session client's cookie jar never leaks a token in."""
    return current_app.test_client().get(url, headers=headers)


def _detail(case_id, headers):
    response = _get(DETAIL.format(case_id), headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["case"]


def _list(headers, query=""):
    response = _get(f"{LIST}{query}", headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _instant(value):
    """A published ISO instant as an aware datetime (the response middleware may move it into the viewer's zone)."""
    return datetime.fromisoformat(value)


def _delivered(db, u, address, *, quantity=1, returned=3, driver_name="Botir"):
    """`u`'s order to `address`, delivered through the staff bot's Delivered tap with `returned` empties typed.
    Returns `(case, driver, order, delivery)`."""
    drv = driver(db, driver_name)
    order, delivery = stop_for(u, address, drv, quantity=quantity)
    response = _driver_delivers(drv, delivery, returned)
    assert response.status_code == 200, response.get_json()
    case = SharedPlaceCase.query.filter_by(order_id=order.id).one()
    return case, drv, order, delivery


def _group(headers, address_ids, label):
    """The admin's place-group create, as PlaceGroupConfirmModal sends it."""
    response = current_app.test_client().post(
        PLACE_GROUPS, json={"addressIds": address_ids, "label": label, "reason": "same building"}, headers=headers
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["place_group_id"]


def _link(headers, primary, secondary):
    """The admin's link confirmation, as LinkedAccountsPanel sends it."""
    response = current_app.test_client().post(
        LINK.format(primary.id), json={"secondaryUserId": secondary.id, "reason": "same person"}, headers=headers
    )
    assert response.status_code == 201, response.get_json()


def _written_case(db, u, address, *, candidates, status="awaiting_admin", asked=None, created_at=None, **fields):
    """A case row as detection writes it, for a frozen list detection could not build (see the module docstring)."""
    order, _ = stop_for(u, address, None, quantity=1)
    case = SharedPlaceCase(
        order_id=order.id,
        address_id=address.id,
        returned_qty=3,
        place_balance_before=0,
        asked_driver_id=asked.id if asked else None,
        candidates=candidates,
        status=status,
        **fields,
    )
    if created_at is not None:
        case.created_at = created_at
    db.session.add(case)
    db.session.commit()
    return case


def _member(address, owner):
    return {
        "address_id": address.id,
        "title": address.title,
        "full_address": address.full_address,
        "owner": {"id": owner.id, "first_name": owner.first_name, "last_name": owner.last_name, "phone": owner.phone},
    }


# --------------------------------------------------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def estate(db):
    """Four open cases and two settled ones, with creation times that make "awaiting admin first" visible: the
    newest case of all awaits the driver and still sorts after both admin cases."""
    now = get_utc_now()
    drv = driver(db, "Botir")
    built = {}
    for key, first, minutes_ago, fields in (
        ("driver_old", "Bekzod", 180, {"status": "awaiting_driver", "asked": drv}),
        ("admin_old", "Dilnoza", 120, {"status": "awaiting_admin"}),
        ("driver_new", "Farrux", 5, {"status": "awaiting_driver", "asked": drv}),
        (
            "admin_new",
            "Gulnora",
            60,
            {"status": "awaiting_admin", "asked": drv, "driver_answer": "none", "answered_at": now},
        ),
        (
            "resolved",
            "Hamid",
            30,
            {
                "status": "resolved",
                "asked": drv,
                "driver_answer": "skipped",
                "answered_at": now,
                "resolution_kind": "own_bottles",
                "resolution_payload": {"kind": "own_bottles"},
                "decision_reason": "his own bottles",
                "decided_at": now,
            },
        ),
        (
            "closed",
            "Iroda",
            20,
            {
                "status": "closed",
                "resolution_payload": {"kind": "close"},
                "decision_reason": "handled by phone",
                "decided_at": now,
            },
        ),
    ):
        u = customer(db, first)
        address = pinned_address(db, u, BASE_LAT, BASE_LNG + 0.001 * len(built))
        built[key] = _written_case(
            db, u, address, candidates=[], created_at=now - timedelta(minutes=minutes_ago), **fields
        )
    return built


def _row_without_instant(row):
    return {key: value for key, value in row.items() if key != "created_at"}


def test_list_open_first_newest_first_with_open_count(db, admin_claim_headers, estate):
    data = _list(admin_claim_headers)

    assert [row["id"] for row in data["cases"]] == [
        estate["admin_new"].id,
        estate["admin_old"].id,
        estate["driver_new"].id,
        estate["driver_old"].id,
    ]
    assert (data["open_count"], data["page"], data["per_page"], data["total"]) == (4, 1, 20, 4)
    assert data["statuses"] == ["awaiting_driver", "awaiting_admin", "resolved", "closed"]
    case = estate["admin_new"]
    [row] = [row for row in data["cases"] if row["id"] == case.id]
    assert _row_without_instant(row) == {
        "id": case.id,
        "status": "awaiting_admin",
        "driver_status": "answered",
        "order_number": case.order.order_number,
        "customer_first_name": "Gulnora",
        "customer_last_name": "Test",
        "address_title": "home",
        "returned_qty": 3,
        # Its frozen list is empty: a "none" typed on the note-only screen (spec §4.4).
        "answer_kind": "no_candidates",
    }
    assert _instant(row["created_at"]) == ensure_utc(case.created_at)
    assert {row["id"]: row["driver_status"] for row in data["cases"]} == {
        estate["admin_new"].id: "answered",
        estate["admin_old"].id: "not_asked",
        estate["driver_new"].id: "awaiting",
        estate["driver_old"].id: "no_answer",
    }

    second = _list(admin_claim_headers, "?status=open&page=2&per_page=2")

    assert [row["id"] for row in second["cases"]] == [estate["driver_new"].id, estate["driver_old"].id]
    assert (second["open_count"], second["page"], second["per_page"], second["total"]) == (4, 2, 2, 4)


def test_list_filters_resolved_and_closed(db, admin_claim_headers, estate):
    resolved = _list(admin_claim_headers, "?status=resolved")
    closed = _list(admin_claim_headers, "?status=closed")

    assert [row["id"] for row in resolved["cases"]] == [estate["resolved"].id]
    assert [row["id"] for row in closed["cases"]] == [estate["closed"].id]
    assert (resolved["total"], resolved["open_count"], closed["total"], closed["open_count"]) == (1, 4, 1, 4)
    assert (resolved["cases"][0]["status"], resolved["cases"][0]["answer_kind"]) == ("resolved", "skipped")
    assert (closed["cases"][0]["status"], closed["cases"][0]["driver_status"]) == ("closed", "not_asked")

    unknown = _get(f"{LIST}?status=everything", admin_claim_headers)

    assert unknown.status_code == 400


# --------------------------------------------------------------------------------------------------------------------
# One case: frozen facts, the driver's answer, the window
# --------------------------------------------------------------------------------------------------------------------


def test_detail_shows_frozen_facts_and_driver_answer(db, admin_claim_headers):
    akmal = customer(db, "Akmal")
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    empties_at(akmal_addr, 8)
    u = customer(db, "Jasur")
    u.last_name = "Karimov"
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    case, drv, order, delivery = _delivered(db, u, address, quantity=2, returned=3)
    answered = current_app.test_client().put(
        ANSWER.format(case.id),
        json={"answer": "picked", "candidate_index": 0, "note": "Akmal's coworker"},
        headers=_bearer(drv),
    )
    assert answered.status_code == 200, answered.get_json()
    db.session.expire_all()
    row = SharedPlaceCase.query.get(case.id)

    detail = _detail(case.id, admin_claim_headers)

    assert _instant(detail.pop("created_at")) == ensure_utc(row.created_at)
    assert _instant(detail["answer"].pop("answered_at")) == ensure_utc(row.answered_at)
    assert detail == {
        "id": case.id,
        "status": "awaiting_admin",
        "driver_status": "answered",
        "order": {"id": order.id, "order_number": order.order_number},
        "delivery_id": delivery.id,
        "customer": {"id": u.id, "first_name": "Jasur", "last_name": "Karimov", "phone": u.phone},
        "address": {
            "id": address.id,
            "title": "home",
            "full_address": address.full_address,
            "latitude": BASE_LAT,
            "longitude": BASE_LNG,
            "apartment_number": None,
            "place_group": None,
            "place_empties_now": -1,  # 0 before, 2 full delivered, 3 empties back
        },
        "driver": {"id": drv.id, "first_name": "Botir"},
        "delivered_qty": 2,
        "returned_qty": 3,
        "place_balance_before": 0,
        "answer": {"kind": "picked", "picked_index": 0, "note": "Akmal's coworker"},
        "candidates": [
            {
                "index": 0,
                "ref": {"kind": "address", "id": akmal_addr.id},
                "first_name": "Akmal",
                "empties": 8,
                "distance_m": 14,
                "live": {
                    "exists": True,
                    "place_group_id": None,
                    "label": None,
                    "empties": 8,
                    "members": [_member(akmal_addr, akmal)],
                },
            }
        ],
        "empty_reason": None,
        "facts": {"linked_accounts": [], "referred_by": None},
        "tryouts": [],
        "move_sources": [],
        "choices": {"kinds": ALL_KINDS, "move_allowed": True, "grouping_done": False},
        "claim": None,
        "resolution": None,
    }


def test_a_note_typed_with_no_candidates_reads_no_candidates_never_none(db, admin_claim_headers):
    """Spec §4.4: the note-only screen files `answer=none` plus the note, and every admin surface says "no nearby
    places", never "None of these". A "none" given over offered places keeps reading "none"."""
    u = customer(db, "Jasur")
    lone_case, lone_driver, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))
    akmal = customer(db, "Akmal")
    empties_at(pinned_address(db, akmal, BASE_LAT + FAR + M14, BASE_LNG), 8)
    v = customer(db, "Bobur")
    offered_case, offered_driver, *_ = _delivered(
        db, v, pinned_address(db, v, BASE_LAT + FAR, BASE_LNG), driver_name="Olim"
    )
    assert (lone_case.candidates, len(offered_case.candidates)) == ([], 1)
    for case, drv, body in (
        (lone_case, lone_driver, {"answer": "none", "note": "ordered before on his wife's phone"}),  # the bot's PUT
        (offered_case, offered_driver, {"answer": "none"}),
    ):
        answered = current_app.test_client().put(ANSWER.format(case.id), json=body, headers=_bearer(drv))
        assert answered.status_code == 200, answered.get_json()
        # The driver's own reply keeps the stored answer.
        assert answered.get_json()["data"]["answer"] == "none"

    rows = {row["id"]: row["answer_kind"] for row in _list(admin_claim_headers)["cases"]}
    lone, offered = _detail(lone_case.id, admin_claim_headers), _detail(offered_case.id, admin_claim_headers)

    assert rows == {lone_case.id: "no_candidates", offered_case.id: "none"}
    assert (lone["answer"]["kind"], lone["answer"]["note"], lone["empty_reason"]) == (
        "no_candidates",
        "ordered before on his wife's phone",
        "none_within_radius",
    )
    assert (offered["answer"]["kind"], offered["empty_reason"]) == ("none", None)


def test_detail_driver_status_no_answer_after_window(db, admin_claim_headers):
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))

    assert _detail(case.id, admin_claim_headers)["driver_status"] == "awaiting"
    assert current_app.config["SHARED_PLACE_DRIVER_ANSWER_WINDOW_S"] == 900

    case.created_at = ensure_utc(case.created_at) - timedelta(minutes=16)
    db.session.commit()

    assert _detail(case.id, admin_claim_headers)["driver_status"] == "no_answer"
    [row] = _list(admin_claim_headers)["cases"]
    assert (row["id"], row["driver_status"], row["answer_kind"]) == (case.id, "no_answer", None)


def test_settled_case_the_driver_never_answered_reads_no_answer(db, admin_claim_headers):
    """Not "awaiting": once an admin settled the case, nobody waits for the driver any more."""
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))
    case.status = "closed"
    case.resolution_payload = {"kind": "close"}
    case.decided_at = get_utc_now()
    db.session.commit()

    assert _detail(case.id, admin_claim_headers)["driver_status"] == "no_answer"


def test_admin_entered_case_reads_not_asked(db, admin_claim_headers):
    u = customer(db, "Dilnoza")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    order, _ = stop_for(u, address, None, quantity=1)
    response = current_app.test_client().put(
        f"/api/v1/admin/orders/{order.id}/status",
        json={"status": "delivered", "bottles_returned": 2},
        headers=admin_claim_headers,
    )
    assert response.status_code == 200, response.get_json()
    case = SharedPlaceCase.query.filter_by(order_id=order.id).one()

    detail = _detail(case.id, admin_claim_headers)

    assert (detail["status"], detail["driver_status"], detail["driver"], detail["delivery_id"]) == (
        "awaiting_admin",
        "not_asked",
        None,
        None,
    )
    assert (detail["answer"], detail["candidates"], detail["empty_reason"]) == (None, [], "none_within_radius")


# --------------------------------------------------------------------------------------------------------------------
# Live facts
# --------------------------------------------------------------------------------------------------------------------


def test_detail_live_facts_linked_referred_and_since_grouped(db, admin_claim_headers):
    akmal = customer(db, "Akmal")
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    empties_at(akmal_addr, 8)
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    case, *_ = _delivered(db, u, address, quantity=1, returned=3)
    sibling = customer(db, "Jasurbek")
    sibling.last_name = "Second"
    rustam = customer(db, "Rustam")
    u.referred_by_user_id = rustam.id
    db.session.commit()
    _link(admin_claim_headers, u, sibling)
    group_id = _group(admin_claim_headers, [case.address_id, akmal_addr.id], "Office 12")

    detail = _detail(case.id, admin_claim_headers)

    assert detail["facts"] == {
        "linked_accounts": [{"id": sibling.id, "first_name": "Jasurbek", "last_name": "Second"}],
        "referred_by": {"id": rustam.id, "first_name": "Rustam", "last_name": "Test"},
    }
    assert detail["address"]["place_group"] == {"id": group_id, "label": "Office 12"}
    assert detail["address"]["place_empties_now"] == 6  # 8 at Akmal's + (1 delivered - 3 back) here
    assert detail["choices"] == {"kinds": ALL_KINDS, "move_allowed": False, "grouping_done": True}
    assert detail["candidates"][0]["live"] == {
        "exists": True,
        "place_group_id": group_id,
        "label": "Office 12",
        "empties": 6,
        "members": [_member(akmal_addr, akmal), _member(address, u)],
    }
    assert detail["move_sources"] == []  # the sibling has no address; the case's own place never counts


def test_same_place_and_are_linked_read_live_state(db, admin_claim_headers):
    """The two yes/no reads the resolve call verifies with, against state the admin endpoints wrote."""
    links = CustomerLinkService()
    akmal = customer(db, "Akmal")
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    sibling = customer(db, "Jasurbek")
    other = customer(db, "Olim")
    other_addr = pinned_address(db, other, BASE_LAT + FAR, BASE_LNG)

    assert (links.same_place(address.id, akmal_addr.id), links.are_linked(u.id, sibling.id)) == (False, False)

    _group(admin_claim_headers, [address.id, akmal_addr.id], "Office 12")
    _link(admin_claim_headers, u, sibling)

    assert links.same_place(address.id, akmal_addr.id) is True
    assert links.same_place(akmal_addr.id, address.id) is True
    assert links.same_place(address.id, other_addr.id) is False
    assert links.same_place(other_addr.id, other_addr.id) is False  # ungrouped: in no place with anyone
    assert links.same_place(address.id, 999999) is False
    assert links.are_linked(u.id, sibling.id) is True
    assert links.are_linked(sibling.id, u.id) is True
    assert links.are_linked(u.id, u.id) is False
    assert links.are_linked(u.id, akmal.id) is False
    assert links.are_linked(akmal.id, other.id) is False  # two unlinked accounts share no (NULL) canonical


def test_move_source_of_mixed_owner_group_is_a_cluster_member_address(db, admin_claim_headers):
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    nodira = customer(db, "Nodira")
    nodira_addr = pinned_address(db, nodira, BASE_LAT + FAR, BASE_LNG)  # the group's LOWEST member, not linked
    jasurbek = customer(db, "Jasurbek")  # Jasur's other account
    jasurbek_addr = pinned_address(db, jasurbek, BASE_LAT + FAR + M14, BASE_LNG)
    group_id = _group(admin_claim_headers, [nodira_addr.id, jasurbek_addr.id], "Flat 7")
    empties_at(nodira_addr, 8)
    case, *_ = _delivered(db, u, address)
    _link(admin_claim_headers, u, jasurbek)
    assert nodira_addr.id < jasurbek_addr.id

    detail = _detail(case.id, admin_claim_headers)

    assert detail["move_sources"] == [
        {
            "address_id": jasurbek_addr.id,
            "place_group_id": group_id,
            "label": "Flat 7",
            "empties": 8,
            "owner_first_name": "Jasurbek",
        }
    ]


def test_detail_move_sources_appear_after_link(db, admin_claim_headers):
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    jasurbek = customer(db, "Jasurbek")
    dacha = pinned_address(db, jasurbek, BASE_LAT + FAR, BASE_LNG)
    empties_at(dacha, 6)
    empty_flat = pinned_address(db, jasurbek, BASE_LAT + 2 * FAR, BASE_LNG)  # no empties: never a source
    # 5 full delivered, 1 empty back: the case's own place now holds 4, and is still never a source.
    case, *_ = _delivered(db, u, address, quantity=5, returned=1)

    before = _detail(case.id, admin_claim_headers)

    assert before["move_sources"] == []
    assert before["address"]["place_empties_now"] == 4

    _link(admin_claim_headers, u, jasurbek)
    after = _detail(case.id, admin_claim_headers)

    assert after["move_sources"] == [
        {"address_id": dacha.id, "place_group_id": None, "label": None, "empties": 6, "owner_first_name": "Jasurbek"}
    ]
    assert empty_flat.id not in [source["address_id"] for source in after["move_sources"]]
    assert after["facts"]["linked_accounts"] == [{"id": jasurbek.id, "first_name": "Jasurbek", "last_name": "Test"}]
    assert after["choices"] == {"kinds": ALL_KINDS, "move_allowed": True, "grouping_done": False}


def test_detail_counts_are_ints(db, admin_claim_headers):
    akmal = customer(db, "Akmal")
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    empties_at(akmal_addr, 8)
    u = customer(db, "Jasur")
    jasurbek = customer(db, "Jasurbek")
    empties_at(pinned_address(db, jasurbek, BASE_LAT + FAR, BASE_LNG), 6)
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG), quantity=2, returned=3)
    _link(admin_claim_headers, u, jasurbek)

    detail = _detail(case.id, admin_claim_headers)

    [candidate] = detail["candidates"]
    [source] = detail["move_sources"]
    counts = {
        "delivered_qty": detail["delivered_qty"],
        "returned_qty": detail["returned_qty"],
        "place_balance_before": detail["place_balance_before"],
        "place_empties_now": detail["address"]["place_empties_now"],
        "candidate.empties": candidate["empties"],
        "candidate.distance_m": candidate["distance_m"],
        "live.empties": candidate["live"]["empties"],
        "move_sources.empties": source["empties"],
    }
    assert counts == {
        "delivered_qty": 2,
        "returned_qty": 3,
        "place_balance_before": 0,
        "place_empties_now": -1,
        "candidate.empties": 8,
        "candidate.distance_m": 14,
        "live.empties": 8,
        "move_sources.empties": 6,
    }
    assert {name: type(value) for name, value in counts.items()} == {name: int for name in counts}


def test_detail_candidate_members_carry_full_rows(db, admin_claim_headers):
    akmal = customer(db, "Akmal")
    akmal.last_name = "Aliyev"
    akmal_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)
    sardor = customer(db, "Sardor")
    sardor_addr = pinned_address(db, sardor, BASE_LAT + M14, BASE_LNG + 0.0001)
    group_id = _group(admin_claim_headers, [akmal_addr.id, sardor_addr.id], "Office 12")
    empties_at(akmal_addr, 8)
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))

    [candidate] = _detail(case.id, admin_claim_headers)["candidates"]

    assert (candidate["index"], candidate["ref"]) == (0, {"kind": "group", "id": group_id})
    assert candidate["live"] == {
        "exists": True,
        "place_group_id": group_id,
        "label": "Office 12",
        "empties": 8,
        "members": [
            {
                "address_id": akmal_addr.id,
                "title": "home",
                "full_address": akmal_addr.full_address,
                "owner": {"id": akmal.id, "first_name": "Akmal", "last_name": "Aliyev", "phone": akmal.phone},
            },
            {
                "address_id": sardor_addr.id,
                "title": "home",
                "full_address": sardor_addr.full_address,
                "owner": {"id": sardor.id, "first_name": "Sardor", "last_name": "Test", "phone": sardor.phone},
            },
        ],
    }


def test_detail_tolerates_missing_or_changed_candidates(db, admin_claim_headers):
    """Review Focus 4: the frozen list survives a deleted address, a dissolved group and an address grouped since."""
    akmal = customer(db, "Akmal")
    gone_addr = pinned_address(db, akmal, BASE_LAT + M14, BASE_LNG)  # order-less, no bottles: deletable
    sardor, olim = customer(db, "Sardor"), customer(db, "Olim")
    sardor_addr = pinned_address(db, sardor, BASE_LAT + 2 * M14, BASE_LNG)
    olim_addr = pinned_address(db, olim, BASE_LAT + 2 * M14, BASE_LNG + 0.0001)
    doomed_group = _group(admin_claim_headers, [sardor_addr.id, olim_addr.id], "Office 12")
    empties_at(sardor_addr, 5)
    kamola = customer(db, "Kamola")
    kamola_addr = pinned_address(db, kamola, BASE_LAT + 3 * M14, BASE_LNG)
    empties_at(kamola_addr, 4)
    frozen = [
        {"ref": {"kind": "address", "id": gone_addr.id}, "first_name": "Akmal", "empties": 8, "distance_m": 14},
        {"ref": {"kind": "group", "id": doomed_group}, "first_name": "Sardor", "empties": 5, "distance_m": 28},
        {"ref": {"kind": "address", "id": kamola_addr.id}, "first_name": "Kamola", "empties": 4, "distance_m": 42},
    ]
    u = customer(db, "Jasur")
    case = _written_case(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG), candidates=frozen)

    AuthService().delete_user_address(akmal.id, gone_addr.id)
    dissolved = current_app.test_client().delete(
        GROUP_MEMBER.format(doomed_group, olim_addr.id), json={"reason": "moved out"}, headers=admin_claim_headers
    )
    assert dissolved.status_code == 200, dissolved.get_json()
    kamola_flat = pinned_address(db, kamola, BASE_LAT + 3 * M14, BASE_LNG + 0.0001)
    new_group = _group(admin_claim_headers, [kamola_addr.id, kamola_flat.id], "Kamola's flat")
    assert UserAddress.query.get(gone_addr.id) is None

    response = _get(DETAIL.format(case.id), admin_claim_headers)

    assert response.status_code == 200, response.get_json()
    candidates = response.get_json()["data"]["case"]["candidates"]
    assert [(c["index"], c["ref"], c["first_name"], c["empties"], c["distance_m"]) for c in candidates] == [
        (i, f["ref"], f["first_name"], f["empties"], f["distance_m"]) for i, f in enumerate(frozen)
    ]
    assert candidates[0]["live"] == GONE
    assert candidates[1]["live"] == GONE
    assert candidates[2]["live"] == {
        "exists": True,
        "place_group_id": new_group,
        "label": "Kamola's flat",
        "empties": 4,
        "members": [_member(kamola_addr, kamola), _member(kamola_flat, kamola)],
    }


def test_detail_lists_tryout_trial_bottles(db, admin_claim_headers, admin_user, sample_product):
    """S9: the trial bottles still out on a try-out converted into this account, read live. A settled try-out stays
    listed with 0 out: the screen must still tell the admin to book +returned with D, not to Close (spec §9)."""
    sample_product.is_tryout_eligible = True
    sample_product.tracks_returnable_bottles = True
    sample_product.returnable_bottles_per_unit = Decimal("1.00")
    sample_product.stock_quantity = 10
    db.session.commit()
    payload = _build_payload(sample_product.id, quantity=2, complete_handoff=True)
    payload["trial_contact"]["phone"] = "+998792219001"  # the +99879 block; the contact phone check accepts it
    payload["address"]["latitude"] = BASE_LAT
    payload["address"]["longitude"] = BASE_LNG
    tryout = TryoutService.create_tryout(payload, admin_user.id)
    converted = TryoutService.convert_tryout(tryout.id, admin_user.id)
    assert converted["action"] == "created_user"
    u = converted["user"]
    case, *_ = _delivered(db, u, UserAddress.query.filter_by(user_id=u.id).one(), quantity=2, returned=2)

    detail = _detail(case.id, admin_claim_headers)

    assert detail["tryouts"] == [
        {"tryout_id": tryout.id, "tryout_number": tryout.tryout_number, "outstanding_bottles_total": 2.0}
    ]
    assert type(detail["tryouts"][0]["outstanding_bottles_total"]) is float

    TryoutService.adjust_bottles(tryout.id, sample_product.id, Decimal("-2"), admin_user.id, notes="settled")

    settled = _detail(case.id, admin_claim_headers)["tryouts"]
    assert settled == [{"tryout_id": tryout.id, "tryout_number": tryout.tryout_number, "outstanding_bottles_total": 0.0}]
    assert type(settled[0]["outstanding_bottles_total"]) is float

    # A try-out that never handed trial bottles over explains none of the empties: not listed.
    no_handoff = TryoutService.create_tryout(
        {**payload, "complete_handoff": False, "items": [{"product_id": sample_product.id, "quantity": 1}]},
        admin_user.id,
    )
    assert TryoutService.convert_tryout(no_handoff.id, admin_user.id)["action"] == "linked_existing_user"

    assert [row["tryout_id"] for row in _detail(case.id, admin_claim_headers)["tryouts"]] == [tryout.id]


# --------------------------------------------------------------------------------------------------------------------
# The claim and the outcome
# --------------------------------------------------------------------------------------------------------------------


def test_detail_shows_an_open_claim_and_offers_only_its_kind(db, admin_claim_headers):
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))
    claim = {"kind": "other_place", "target_user_id": 21, "move_count": 3, "source_address_id": 40}
    case.resolution_kind, case.resolution_payload = "other_place", claim
    db.session.commit()

    detail = _detail(case.id, admin_claim_headers)

    assert detail["claim"] == {"kind": "other_place", "payload": claim}
    assert detail["choices"] == {"kinds": ["other_place"], "move_allowed": True, "grouping_done": False}
    assert detail["resolution"] is None


def test_detail_of_a_settled_case_shows_the_outcome_and_offers_nothing(db, admin_claim_headers, admin_user):
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))
    closed = customer(db, "Kamol")
    closed_case, *_ = _delivered(db, closed, pinned_address(db, closed, BASE_LAT + FAR, BASE_LNG), driver_name="Olim")
    decided, admin_id = get_utc_now(), admin_user.id  # read before the rows change: a lazy load autoflushes
    case.status, case.resolution_kind, case.resolution_payload = "resolved", "own_bottles", {"kind": "own_bottles"}
    case.decision_reason, case.decided_by_user_id, case.decided_at = "his own bottles", admin_id, decided
    closed_case.status, closed_case.resolution_payload = "closed", {"kind": "close"}
    closed_case.decision_reason, closed_case.decided_at = "handled by phone", decided
    db.session.commit()

    resolved_detail = _detail(case.id, admin_claim_headers)
    closed_detail = _detail(closed_case.id, admin_claim_headers)

    for detail in (resolved_detail, closed_detail):
        assert _instant(detail["resolution"].pop("decided_at")) == ensure_utc(decided)
        assert detail["choices"] == {"kinds": [], "move_allowed": False, "grouping_done": False}
        assert detail["claim"] is None
    assert resolved_detail["resolution"] == {
        "kind": "own_bottles",
        "payload": {"kind": "own_bottles"},
        "reason": "his own bottles",
        "decided_by": {"id": admin_id, "first_name": "Admin"},
        "resulting_group_id": None,
    }
    assert closed_detail["resolution"] == {
        "kind": "close",
        "payload": {"kind": "close"},
        "reason": "handled by phone",
        "decided_by": None,
        "resulting_group_id": None,
    }


# --------------------------------------------------------------------------------------------------------------------
# Who may read
# --------------------------------------------------------------------------------------------------------------------


def test_list_and_detail_on_real_postgres(pg_db):
    """The same reads on a migrated Postgres (real CHECKs, the JSON column, the CASE ordering), driven end to end:
    a driver case with a mixed-owner group candidate and a linked account's grouped place, and an admin-entered
    case that sorts first."""
    from shared.enums import UserRole, UserType
    from tests.integration.reorder_reminder_builders import make_customer

    admin = make_customer(pg_db, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)
    headers = {
        "Authorization": "Bearer " + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }
    akmal, sardor = customer(pg_db, "Akmal"), customer(pg_db, "Sardor")
    akmal_addr = pinned_address(pg_db, akmal, BASE_LAT + M14, BASE_LNG)
    sardor_addr = pinned_address(pg_db, sardor, BASE_LAT + M14, BASE_LNG + 0.0001)
    office = _group(headers, [akmal_addr.id, sardor_addr.id], "Office 12")
    empties_at(akmal_addr, 8)
    nodira, jasurbek = customer(pg_db, "Nodira"), customer(pg_db, "Jasurbek")
    nodira_addr = pinned_address(pg_db, nodira, BASE_LAT + FAR, BASE_LNG)
    jasurbek_addr = pinned_address(pg_db, jasurbek, BASE_LAT + FAR + M14, BASE_LNG)
    flat = _group(headers, [nodira_addr.id, jasurbek_addr.id], "Flat 7")
    empties_at(nodira_addr, 5)
    u = customer(pg_db, "Jasur")
    case, *_ = _delivered(pg_db, u, pinned_address(pg_db, u, BASE_LAT, BASE_LNG), quantity=2, returned=3)
    _link(headers, u, jasurbek)
    dilnoza = customer(pg_db, "Dilnoza")
    later_order, _ = stop_for(dilnoza, pinned_address(pg_db, dilnoza, BASE_LAT + 2 * FAR, BASE_LNG), None, quantity=1)
    entered = current_app.test_client().put(
        f"/api/v1/admin/orders/{later_order.id}/status",
        json={"status": "delivered", "bottles_returned": 2},
        headers=headers,
    )
    assert entered.status_code == 200, entered.get_json()
    admin_case = SharedPlaceCase.query.filter_by(order_id=later_order.id).one()

    listed = _list(headers)
    detail = _detail(case.id, headers)

    assert [(row["id"], row["driver_status"]) for row in listed["cases"]] == [
        (admin_case.id, "not_asked"),
        (case.id, "awaiting"),
    ]
    assert (listed["open_count"], listed["total"]) == (2, 2)
    [candidate] = detail["candidates"]
    assert candidate["ref"] == {"kind": "group", "id": office}
    assert (candidate["live"]["exists"], candidate["live"]["label"], candidate["live"]["empties"]) == (
        True,
        "Office 12",
        8,
    )
    assert [m["address_id"] for m in candidate["live"]["members"]] == [akmal_addr.id, sardor_addr.id]
    assert detail["move_sources"] == [
        {
            "address_id": jasurbek_addr.id,
            "place_group_id": flat,
            "label": "Flat 7",
            "empties": 5,
            "owner_first_name": "Jasurbek",
        }
    ]
    assert detail["facts"]["linked_accounts"] == [{"id": jasurbek.id, "first_name": "Jasurbek", "last_name": "Test"}]
    assert (detail["delivered_qty"], detail["address"]["place_empties_now"], detail["place_balance_before"]) == (
        2,
        -1,
        0,
    )
    assert detail["choices"] == {"kinds": ALL_KINDS, "move_allowed": True, "grouping_done": False}


def test_missing_case_is_404(db, admin_claim_headers):
    response = _get(DETAIL.format(424242), admin_claim_headers)

    assert response.status_code == 404
    assert response.get_json()["error_code"] == "SHARED_PLACE_CASE_NOT_FOUND"


def test_manager_gets_403_on_every_route(db, admin_claim_headers, manager_claim_headers):
    u = customer(db, "Jasur")
    case, *_ = _delivered(db, u, pinned_address(db, u, BASE_LAT, BASE_LNG))

    statuses = [_get(url, manager_claim_headers).status_code for url in (LIST, DETAIL.format(case.id))]

    assert statuses == [403, 403]
    assert [_get(url, admin_claim_headers).status_code for url in (LIST, DETAIL.format(case.id))] == [200, 200]
