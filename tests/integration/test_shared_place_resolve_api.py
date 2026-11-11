"""The admin's resolution of a shared-place case (spec 2026-11-10 §4.5; §9 binds): one POST settles a case with one of
six outcomes. It verifies A, B and C against live state, and books C's, D's and E's bottle moves itself.

Driven through `POST /admin/shared-place-cases/<id>/resolve`, the call the Driver reports tab makes, with the body
it sends. Grouping and linking go through the real place-group and link endpoints first, as the case screen's
dialogs do, and cases open through the staff bot's Delivered tap. Every move is keyed `shared_place:{case}:{leg}`,
so a double submit, or a retry after a half-done move, books each leg once.
"""

import logging
from decimal import Decimal

import pytest
from flask import current_app

from business_app.models.bottle import BottleLedger
from business_app.models.shared_place_case import SharedPlaceCase
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import BottleLedgerEventType
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    empties_at,
    pinned_address,
    stop_for,
)
from tests.integration.test_shared_place_admin_review_api import (
    FAR,
    M14,
    _bearer,
    _delivered,
    _detail,
    _group,
    _link,
)
from tests.integration.test_shared_place_detection import _driver_delivers
from tests.integration.test_staff_bot_place_full_e2e import _pair

pytestmark = pytest.mark.integration

RESOLVE = "/api/v1/admin/shared-place-cases/{}/resolve"
QUESTION = "/api/v1/staff/delivery/{}/shared-place-question"
ANSWER = "/api/v1/staff/shared-place-cases/{}/answer"
NOTE = "/api/v1/staff/shared-place-cases/{}/note"
PLACE_GROUPS = "/api/v1/admin/place-groups"
RESOLVE_LOGGER = "business_app.services.shared_place.resolve"
INVALID = (400, "SHARED_PLACE_RESOLVE_INVALID")
NOT_VERIFIED = (409, "SHARED_PLACE_NOT_VERIFIED")
CONFLICT = (409, "SHARED_PLACE_RESOLUTION_CONFLICT")


# --------------------------------------------------------------------------------------------------------------------
# Helpers: every state change goes through the endpoint the admin UI or the staff bot calls
# --------------------------------------------------------------------------------------------------------------------


def _resolve(case_id, body, headers):
    """The case screen's resolve call. A fresh client, so the session client's cookie jar never leaks a token in."""
    return current_app.test_client().post(RESOLVE.format(case_id), json=body, headers=headers)


def _resolved(case_id, body, headers):
    response = _resolve(case_id, body, headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["case"]


def _refused(case_id, body, headers):
    """`(status, error_code)` of a refused resolve."""
    response = _resolve(case_id, body, headers)
    return response.status_code, response.get_json().get("error_code")


def _legs(case_id):
    """The ledger rows the resolve booked for this case: `{key: (address_id, quantity)}`."""
    rows = BottleLedger.query.filter(BottleLedger.idempotency_key.like(f"shared_place:{case_id}:%")).all()
    return {row.idempotency_key: (row.address_id, row.quantity) for row in rows}


def _row(db, case_id):
    db.session.expire_all()
    return SharedPlaceCase.query.get(case_id)


def _assert_still_open_without_claim(db, case_id):
    row = _row(db, case_id)
    assert (row.status, row.resolution_kind, row.resolution_payload, row.decided_at) == (
        "awaiting_driver",
        None,
        None,
        None,
    )
    assert _legs(case_id) == {}


def _without_instant(resolution):
    return {key: value for key, value in resolution.items() if key != "decided_at"}


def _neighbour(db, first_name="Akmal", *, lat=BASE_LAT + M14, empties=8):
    """Another customer's ungrouped address with `empties` on record: the place the driver is offered."""
    neighbour = customer(db, first_name)
    address = pinned_address(db, neighbour, lat, BASE_LNG)
    empties_at(address, empties)
    return neighbour, address


def _jasur(db, *, quantity=1, returned=3):
    """Jasur's first order, delivered with `returned` empties typed: the case. Returns `(customer, address, case)`."""
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    case, *_ = _delivered(db, u, address, quantity=quantity, returned=returned)
    return u, address, case


def _second_account(db, *, empties=8):
    """Jasurbek (Jasur's other account) with his dacha far away holding `empties`. Not linked yet."""
    return _neighbour(db, "Jasurbek", lat=BASE_LAT + FAR, empties=empties)


def _move(target, source, k, reason="his dacha's bottles"):
    return {
        "kind": "other_place",
        "target_user_id": target.id,
        "move_count": k,
        "source_address_id": source.id,
        "reason": reason,
    }


@pytest.fixture
def in_leg_fails_once(monkeypatch):
    """`admin_adjust_balance` raises once, on the first `:in` leg: a move half done. Returns the keys it refused."""
    original = BottleTrackingService.admin_adjust_balance
    refused = []

    def flaky(self, user_id, address_id, adjustment, actor_user_id, notes, *, idempotency_key=None):
        if idempotency_key is not None and idempotency_key.endswith(":in") and not refused:
            refused.append(idempotency_key)
            raise RuntimeError("the database went away mid-move")
        return original(self, user_id, address_id, adjustment, actor_user_id, notes, idempotency_key=idempotency_key)

    monkeypatch.setattr(BottleTrackingService, "admin_adjust_balance", flaky)
    return refused


# --------------------------------------------------------------------------------------------------------------------
# A. Shared place
# --------------------------------------------------------------------------------------------------------------------


def test_shared_place_resolves_after_grouping(db, admin_claim_headers, admin_user):
    _, akmal_addr = _neighbour(db)
    _, address, case = _jasur(db, quantity=1, returned=3)
    group_id = _group(admin_claim_headers, [address.id, akmal_addr.id], "Office 12")

    detail = _resolved(
        case.id,
        {"kind": "shared_place", "target_address_id": akmal_addr.id, "reason": "coworkers"},
        admin_claim_headers,
    )

    assert detail["status"] == "resolved"
    assert (detail["claim"], detail["choices"]["kinds"]) == (None, [])
    assert detail["resolution"]["decided_at"] is not None
    assert _without_instant(detail["resolution"]) == {
        "kind": "shared_place",
        "payload": {"kind": "shared_place", "target_address_id": akmal_addr.id},
        "reason": "coworkers",
        "decided_by": {"id": admin_user.id, "first_name": "Admin"},
        "resulting_group_id": group_id,
    }
    row = _row(db, case.id)
    assert (row.resolution_kind, row.decided_by_user_id, row.resulting_group_id) == (
        "shared_place",
        admin_user.id,
        group_id,
    )
    assert _legs(case.id) == {}
    assert _pair(address.id) == (Decimal("6"), Decimal("6"))  # 8 at Akmal's + 1 delivered - 3 back


def test_shared_place_refused_when_not_grouped(db, admin_claim_headers):
    """Review Focus 4: the resolve verifies against live state only."""
    _, akmal_addr = _neighbour(db)
    _, sardor_addr = _neighbour(db, "Sardor", lat=BASE_LAT + FAR)
    _, address, case = _jasur(db)
    with_target = {"kind": "shared_place", "target_address_id": akmal_addr.id, "reason": "coworkers"}
    without_target = {"kind": "shared_place", "reason": "coworkers"}

    assert _refused(case.id, with_target, admin_claim_headers) == NOT_VERIFIED
    assert _refused(case.id, without_target, admin_claim_headers) == NOT_VERIFIED

    _group(admin_claim_headers, [address.id, sardor_addr.id], "Sardor's office")  # grouped, but not with Akmal

    assert _refused(case.id, with_target, admin_claim_headers) == NOT_VERIFIED
    _assert_still_open_without_claim(db, case.id)


def test_grouped_elsewhere_confirms_shared_place_without_target(db, admin_claim_headers):
    """Review Focus 4 / spec §9: a grouping already made (here, before the review) is confirmed without regrouping."""
    _, sardor_addr = _neighbour(db, "Sardor", lat=BASE_LAT + FAR)
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    case, *_ = _delivered(db, u, address)
    group_id = _group(admin_claim_headers, [address.id, sardor_addr.id], "Sardor's office")
    assert _detail(case.id, admin_claim_headers)["choices"]["grouping_done"] is True

    detail = _resolved(case.id, {"kind": "shared_place", "reason": "r"}, admin_claim_headers)

    assert (detail["status"], detail["resolution"]["resulting_group_id"]) == ("resolved", group_id)
    assert detail["resolution"]["payload"] == {"kind": "shared_place"}


def test_a_one_member_group_is_not_grouped_for_the_screen_or_the_resolve(db, admin_claim_headers):
    """One predicate (`is_in_live_place_group`): a group left with only the case address is no place. The screen does
    not offer "Confirm" for it, and A is refused for it, consistently."""
    _, sardor_addr = _neighbour(db, "Sardor", lat=BASE_LAT + FAR)
    _, address, case = _jasur(db)
    _group(admin_claim_headers, [address.id, sardor_addr.id], "Sardor's office")
    assert _detail(case.id, admin_claim_headers)["choices"]["grouping_done"] is True
    # The group keeps one member, the case address (a written row: no admin route leaves a group with one).
    UserAddress.query.filter_by(id=sardor_addr.id).update({"address_group_id": None})
    db.session.commit()
    assert CustomerLinkService().get_address_place_group_id(address.id) is not None

    choices = _detail(case.id, admin_claim_headers)["choices"]

    assert (choices["grouping_done"], choices["move_allowed"]) == (False, True)
    assert _refused(case.id, {"kind": "shared_place", "reason": "r"}, admin_claim_headers) == NOT_VERIFIED
    _assert_still_open_without_claim(db, case.id)


def test_a_and_b_are_never_gated_by_what_the_screen_offers(db, admin_claim_headers, monkeypatch):
    """Spec §9: A and B are verified against live state only. A screen rule that offers nothing still refuses D."""
    from business_app.services.shared_place import resolve as resolve_module

    def offers_nothing(case):
        return {"kinds": [], "move_allowed": False, "grouping_done": True}

    monkeypatch.setattr(resolve_module, "allowed_choices", offers_nothing)
    _, akmal_addr = _neighbour(db)
    _, address, case = _jasur(db)
    _group(admin_claim_headers, [address.id, akmal_addr.id], "Office 12")

    assert _refused(case.id, {"kind": "own_bottles", "reason": "r"}, admin_claim_headers) == INVALID
    assert _resolved(case.id, {"kind": "shared_place", "reason": "r"}, admin_claim_headers)["status"] == "resolved"


# --------------------------------------------------------------------------------------------------------------------
# B. Same person, same home
# --------------------------------------------------------------------------------------------------------------------


def test_same_home_requires_link_and_group(db, admin_claim_headers):
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    jasurbek, jasurbek_addr = _neighbour(db, "Jasurbek")  # his other account, at the same door
    case, *_ = _delivered(db, u, address)
    body = {"kind": "same_home", "target_user_id": jasurbek.id, "target_address_id": jasurbek_addr.id, "reason": "r"}
    no_account = {"kind": "same_home", "reason": "r"}

    # Checked in order: the grouping first, then the named account (400), then its link (409).
    assert _refused(case.id, no_account, admin_claim_headers) == NOT_VERIFIED  # not grouped
    assert _refused(case.id, body, admin_claim_headers) == NOT_VERIFIED  # neither yet

    group_id = _group(admin_claim_headers, [address.id, jasurbek_addr.id], "Jasur's flat")

    assert _refused(case.id, no_account, admin_claim_headers) == INVALID  # grouped, no account named
    assert _refused(case.id, body, admin_claim_headers) == NOT_VERIFIED  # grouped, not linked
    _assert_still_open_without_claim(db, case.id)

    _link(admin_claim_headers, u, jasurbek)
    detail = _resolved(case.id, body, admin_claim_headers)

    assert (detail["status"], detail["resolution"]["kind"], detail["resolution"]["resulting_group_id"]) == (
        "resolved",
        "same_home",
        group_id,
    )
    assert detail["resolution"]["payload"] == {
        "kind": "same_home",
        "target_user_id": jasurbek.id,
        "target_address_id": jasurbek_addr.id,
    }
    assert _legs(case.id) == {}


def test_same_home_refused_when_linked_but_not_grouped(db, admin_claim_headers):
    """The half-finished B (spec §4.5): linked, then the grouping dialog cancelled. The case stays open."""
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    jasurbek, _ = _neighbour(db, "Jasurbek")
    case, *_ = _delivered(db, u, address)
    _link(admin_claim_headers, u, jasurbek)

    assert (
        _refused(case.id, {"kind": "same_home", "target_user_id": jasurbek.id, "reason": "r"}, admin_claim_headers)
        == NOT_VERIFIED
    )
    _assert_still_open_without_claim(db, case.id)


# --------------------------------------------------------------------------------------------------------------------
# C. Same person, another place
# --------------------------------------------------------------------------------------------------------------------


def test_other_place_link_only_writes_no_ledger(db, admin_claim_headers):
    u, address, case = _jasur(db)
    jasurbek, dacha = _second_account(db)
    link_only = {"kind": "other_place", "target_user_id": jasurbek.id, "reason": "his second home"}

    assert _refused(case.id, {"kind": "other_place", "reason": "r"}, admin_claim_headers) == INVALID
    assert _refused(case.id, link_only, admin_claim_headers) == NOT_VERIFIED

    _link(admin_claim_headers, u, jasurbek)
    detail = _resolved(case.id, link_only, admin_claim_headers)

    assert (detail["status"], detail["resolution"]["payload"], detail["resolution"]["resulting_group_id"]) == (
        "resolved",
        {"kind": "other_place", "target_user_id": jasurbek.id},
        None,
    )
    assert _legs(case.id) == {}
    assert _pair(dacha.id) == (Decimal("8"), Decimal("8"))
    assert _pair(address.id) == (Decimal("-2"), Decimal("-2"))


def test_other_place_move_books_two_keyed_legs(db, admin_claim_headers, admin_user):
    u, address, case = _jasur(db, quantity=1, returned=3)
    jasurbek, dacha = _second_account(db, empties=8)
    _link(admin_claim_headers, u, jasurbek)

    detail = _resolved(case.id, _move(jasurbek, dacha, 3), admin_claim_headers)

    assert detail["status"] == "resolved"
    assert detail["resolution"]["payload"] == {
        "kind": "other_place",
        "target_user_id": jasurbek.id,
        "move_count": 3,
        "source_address_id": dacha.id,
    }
    assert _legs(case.id) == {
        f"shared_place:{case.id}:out": (dacha.id, Decimal("-3")),
        f"shared_place:{case.id}:in": (address.id, Decimal("3")),
    }
    rows = BottleLedger.query.filter(BottleLedger.idempotency_key.like(f"shared_place:{case.id}:%")).all()
    assert {(row.event_type, row.actor_user_id, row.notes) for row in rows} == {
        (BottleLedgerEventType.ADMIN_ADJUSTMENT, admin_user.id, f"Case #{case.id}: his dacha's bottles")
    }
    assert _pair(dacha.id) == (Decimal("5"), Decimal("5"))
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))  # -2 + 3
    assert _detail(case.id, admin_claim_headers)["move_sources"] == [
        {"address_id": dacha.id, "place_group_id": None, "label": None, "empties": 5, "owner_first_name": "Jasurbek"}
    ]


def _move_above_the_source(db, headers, u, address, jasurbek, dacha):
    return _move(jasurbek, dacha, 9)


def _move_from_outside_the_cluster(db, headers, u, address, jasurbek, dacha):
    _, olim_addr = _neighbour(db, "Olim", lat=BASE_LAT + 2 * FAR)  # holds 8, never linked
    return _move(jasurbek, olim_addr, 3)


def _move_into_a_grouped_case_address(db, headers, u, address, jasurbek, dacha):
    _, akmal_addr = _neighbour(db)
    _group(headers, [address.id, akmal_addr.id], "Office 12")
    return _move(jasurbek, dacha, 3)


def _move_without_a_source(db, headers, u, address, jasurbek, dacha):
    return {"kind": "other_place", "target_user_id": jasurbek.id, "move_count": 3, "reason": "r"}


@pytest.mark.parametrize(
    "build",
    [
        _move_above_the_source,
        _move_from_outside_the_cluster,
        _move_into_a_grouped_case_address,
        _move_without_a_source,
    ],
    ids=["above_the_source", "outside_the_cluster", "case_address_grouped", "no_source"],
)
def test_other_place_move_refused(db, admin_claim_headers, build):
    u, address, case = _jasur(db)
    jasurbek, dacha = _second_account(db, empties=8)
    _link(admin_claim_headers, u, jasurbek)
    body = build(db, admin_claim_headers, u, address, jasurbek, dacha)

    assert _refused(case.id, body, admin_claim_headers) == INVALID
    _assert_still_open_without_claim(db, case.id)
    assert _pair(dacha.id) == (Decimal("8"), Decimal("8"))


def test_other_place_move_refused_from_the_case_place_itself(db, admin_claim_headers):
    """The source place must differ from the case place, even when that place holds enough empties."""
    u, address, case = _jasur(db, quantity=5, returned=1)  # the case place now holds 4
    jasurbek, _ = _second_account(db)
    _link(admin_claim_headers, u, jasurbek)

    assert _refused(case.id, _move(jasurbek, address, 1), admin_claim_headers) == INVALID
    _assert_still_open_without_claim(db, case.id)


def test_other_place_move_from_grouped_source(db, admin_claim_headers):
    """A mixed-owner group whose lowest member is not the linked account's: the move source the detail publishes
    (the linked account's own address in that group) is accepted, and the other owner's address is not."""
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    nodira = customer(db, "Nodira")
    nodira_addr = pinned_address(db, nodira, BASE_LAT + FAR, BASE_LNG)  # the group's LOWEST member, not linked
    jasurbek = customer(db, "Jasurbek")
    jasurbek_addr = pinned_address(db, jasurbek, BASE_LAT + FAR + M14, BASE_LNG)
    group_id = _group(admin_claim_headers, [nodira_addr.id, jasurbek_addr.id], "Flat 7")
    empties_at(nodira_addr, 8)
    case, *_ = _delivered(db, u, address)
    _link(admin_claim_headers, u, jasurbek)
    [source] = _detail(case.id, admin_claim_headers)["move_sources"]
    assert (source["address_id"], source["place_group_id"], nodira_addr.id < jasurbek_addr.id) == (
        jasurbek_addr.id,
        group_id,
        True,
    )

    assert _refused(case.id, _move(jasurbek, nodira_addr, 3), admin_claim_headers) == INVALID

    detail = _resolved(case.id, _move(jasurbek, jasurbek_addr, 3), admin_claim_headers)

    assert detail["status"] == "resolved"
    assert _legs(case.id) == {
        f"shared_place:{case.id}:out": (jasurbek_addr.id, Decimal("-3")),
        f"shared_place:{case.id}:in": (address.id, Decimal("3")),
    }
    assert _pair(nodira_addr.id) == (Decimal("5"), Decimal("5"))  # the whole Flat 7 place
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))


# --------------------------------------------------------------------------------------------------------------------
# D. Own bottles, E. Wrong count, F. Close
# --------------------------------------------------------------------------------------------------------------------


def test_own_bottles_adds_returned(db, admin_claim_headers):
    _, address, case = _jasur(db, quantity=1, returned=3)

    detail = _resolved(case.id, {"kind": "own_bottles", "reason": "bought them at the market"}, admin_claim_headers)

    assert (detail["status"], detail["resolution"]["kind"], detail["resolution"]["payload"]) == (
        "resolved",
        "own_bottles",
        {"kind": "own_bottles"},
    )
    assert _legs(case.id) == {f"shared_place:{case.id}:in": (address.id, Decimal("3"))}
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))  # -2 + 3


def test_wrong_count_adjusts_difference(db, admin_claim_headers):
    _, address, case = _jasur(db, quantity=1, returned=5)
    assert _pair(address.id) == (Decimal("-4"), Decimal("-4"))

    assert (
        _refused(case.id, {"kind": "wrong_count", "actual_returned": 5, "reason": "r"}, admin_claim_headers) == INVALID
    )
    assert _refused(case.id, {"kind": "wrong_count", "reason": "r"}, admin_claim_headers) == INVALID
    _assert_still_open_without_claim(db, case.id)

    detail = _resolved(
        case.id, {"kind": "wrong_count", "actual_returned": 0, "reason": "mistyped"}, admin_claim_headers
    )

    assert (detail["status"], detail["resolution"]["payload"]) == (
        "resolved",
        {"kind": "wrong_count", "actual_returned": 0},
    )
    assert _legs(case.id) == {f"shared_place:{case.id}:fix": (address.id, Decimal("5"))}
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))  # -4 + 5


def test_close_closes_without_ledger(db, admin_claim_headers, admin_user):
    _, address, case = _jasur(db)

    detail = _resolved(case.id, {"kind": "close", "reason": "handled by phone"}, admin_claim_headers)

    assert detail["status"] == "closed"
    assert _without_instant(detail["resolution"]) == {
        "kind": "close",
        "payload": {"kind": "close"},
        "reason": "handled by phone",
        "decided_by": {"id": admin_user.id, "first_name": "Admin"},
        "resulting_group_id": None,
    }
    row = _row(db, case.id)
    assert (row.status, row.resolution_kind, row.resolution_payload, row.decided_by_user_id) == (
        "closed",
        None,
        {"kind": "close"},
        admin_user.id,
    )
    assert row.decided_at is not None
    assert _legs(case.id) == {}
    assert _pair(address.id) == (Decimal("-2"), Decimal("-2"))


def test_double_close_is_a_replay(db, admin_claim_headers):
    _, _, case = _jasur(db)
    body = {"kind": "close", "reason": "handled by phone"}

    first = _resolved(case.id, body, admin_claim_headers)
    second = _resolved(case.id, body, admin_claim_headers)

    assert second == first
    row = _row(db, case.id)
    assert (row.status, row.resolution_kind, row.resolution_payload) == ("closed", None, {"kind": "close"})


# --------------------------------------------------------------------------------------------------------------------
# Replays, conflicts and half-done moves
# --------------------------------------------------------------------------------------------------------------------


def test_double_submit_books_nothing_twice(db, admin_claim_headers):
    u, address, case = _jasur(db)
    jasurbek, dacha = _second_account(db, empties=8)
    _link(admin_claim_headers, u, jasurbek)

    first = _resolved(case.id, _move(jasurbek, dacha, 3), admin_claim_headers)
    second = _resolved(case.id, _move(jasurbek, dacha, 3), admin_claim_headers)

    assert second == first  # the stored outcome, decided_at included, is untouched
    assert _legs(case.id) == {
        f"shared_place:{case.id}:out": (dacha.id, Decimal("-3")),
        f"shared_place:{case.id}:in": (address.id, Decimal("3")),
    }
    assert _pair(dacha.id) == (Decimal("5"), Decimal("5"))
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))


def test_different_second_resolution_conflicts(db, admin_claim_headers):
    _, address, case = _jasur(db)
    _resolved(case.id, {"kind": "own_bottles", "reason": "his own"}, admin_claim_headers)

    for body in (
        {"kind": "wrong_count", "actual_returned": 0, "reason": "mistyped"},
        {"kind": "close", "reason": "handled by phone"},
        {"kind": "shared_place", "reason": "coworkers"},
    ):
        assert _refused(case.id, body, admin_claim_headers) == CONFLICT, body

    # The reason is not part of the claim: the same outcome with other words is a replay.
    replayed = _resolved(case.id, {"kind": "own_bottles", "reason": "other words"}, admin_claim_headers)
    assert replayed["resolution"]["reason"] == "his own"
    assert _legs(case.id) == {f"shared_place:{case.id}:in": (address.id, Decimal("3"))}


def test_a_closed_case_refuses_a_resolution(db, admin_claim_headers):
    _, _, case = _jasur(db)
    _resolved(case.id, {"kind": "close", "reason": "handled by phone"}, admin_claim_headers)

    assert _refused(case.id, {"kind": "own_bottles", "reason": "his own"}, admin_claim_headers) == CONFLICT
    assert _legs(case.id) == {}


def _half_done_move(db, headers, in_leg_fails_once, *, k):
    """A move whose `in` leg failed: the claim is stored, the `out` leg booked. Returns `(case, address, jasurbek,
    dacha)`."""
    u, address, case = _jasur(db)
    jasurbek, dacha = _second_account(db, empties=8)
    _link(headers, u, jasurbek)

    response = _resolve(case.id, _move(jasurbek, dacha, k), headers)

    assert response.status_code == 500
    assert in_leg_fails_once == [f"shared_place:{case.id}:in"]
    detail = _detail(case.id, headers)
    assert (detail["status"], detail["resolution"]) == ("awaiting_driver", None)
    assert detail["claim"] == {
        "kind": "other_place",
        "payload": {
            "kind": "other_place",
            "target_user_id": jasurbek.id,
            "move_count": k,
            "source_address_id": dacha.id,
        },
    }
    assert detail["choices"]["kinds"] == ["other_place"]
    assert _legs(case.id) == {f"shared_place:{case.id}:out": (dacha.id, Decimal(-k))}
    return case, address, jasurbek, dacha


def test_half_done_move_finishes_on_retry_and_refuses_a_different_K(db, admin_claim_headers, in_leg_fails_once):
    case, address, jasurbek, dacha = _half_done_move(db, admin_claim_headers, in_leg_fails_once, k=3)

    assert _refused(case.id, _move(jasurbek, dacha, 2), admin_claim_headers) == CONFLICT

    detail = _resolved(case.id, _move(jasurbek, dacha, 3), admin_claim_headers)

    assert (detail["status"], detail["claim"]) == ("resolved", None)
    assert _legs(case.id) == {
        f"shared_place:{case.id}:out": (dacha.id, Decimal("-3")),
        f"shared_place:{case.id}:in": (address.id, Decimal("3")),
    }
    assert _pair(dacha.id) == (Decimal("5"), Decimal("5"))
    assert _pair(address.id) == (Decimal("1"), Decimal("1"))


def test_full_move_half_done_finishes_on_retry(db, admin_claim_headers, in_leg_fails_once):
    """K is every empty the source held. Once `out` booked, the source holds 0 < K, so a retry that re-verified the
    cap would refuse: a stored claim is not re-verified."""
    case, address, jasurbek, dacha = _half_done_move(db, admin_claim_headers, in_leg_fails_once, k=8)
    assert _pair(dacha.id) == (Decimal("0"), Decimal("0"))

    detail = _resolved(case.id, _move(jasurbek, dacha, 8), admin_claim_headers)

    assert detail["status"] == "resolved"
    assert _legs(case.id) == {
        f"shared_place:{case.id}:out": (dacha.id, Decimal("-8")),
        f"shared_place:{case.id}:in": (address.id, Decimal("8")),
    }
    assert _pair(dacha.id) == (Decimal("0"), Decimal("0"))
    assert _pair(address.id) == (Decimal("6"), Decimal("6"))  # -2 + 8


def test_close_refused_while_a_claim_exists(db, admin_claim_headers, in_leg_fails_once):
    case, *_ = _half_done_move(db, admin_claim_headers, in_leg_fails_once, k=3)

    assert _refused(case.id, {"kind": "close", "reason": "never mind"}, admin_claim_headers) == CONFLICT
    assert _row(db, case.id).status == "awaiting_driver"


def test_different_kind_while_claimed_is_409(db, admin_claim_headers, in_leg_fails_once):
    """Not the 400 of "a kind not offered": the claim mismatch is checked first."""
    case, _, _, dacha = _half_done_move(db, admin_claim_headers, in_leg_fails_once, k=3)

    for body in (
        {"kind": "own_bottles", "reason": "his own"},
        {"kind": "wrong_count", "actual_returned": 0, "reason": "mistyped"},
        {"kind": "shared_place", "reason": "coworkers"},
    ):
        assert _refused(case.id, body, admin_claim_headers) == CONFLICT, body
    assert _row(db, case.id).resolution_kind == "other_place"
    assert _legs(case.id) == {f"shared_place:{case.id}:out": (dacha.id, Decimal("-3"))}


# --------------------------------------------------------------------------------------------------------------------
# The request: shape, guard, missing case, log
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"kind": "own_bottles"},
        {"kind": "own_bottles", "reason": "   "},
        {"kind": "own_bottles", "reason": "x" * 501},
        {"kind": "everything", "reason": "r"},
        {"kind": "own_bottles", "reason": "r", "note": "an unknown key"},
        {"kind": "other_place", "reason": "r", "target_user_id": "21"},
        {"kind": "other_place", "reason": "r", "target_user_id": 21, "move_count": 0, "source_address_id": 40},
        {"kind": "other_place", "reason": "r", "target_user_id": 21, "source_address_id": 40},
        {"kind": "wrong_count", "reason": "r", "actual_returned": -1},
        {"kind": "wrong_count", "reason": "r", "actual_returned": 1.5},
        {"kind": "close", "reason": "r", "move_count": 3},
        {"kind": "own_bottles", "reason": "r", "actual_returned": 0},
        {"kind": "shared_place", "reason": "r", "target_user_id": 21},
    ],
    ids=[
        "no_body",
        "empty",
        "no_reason",
        "blank_reason",
        "reason_too_long",
        "unknown_kind",
        "unknown_key",
        "string_id",
        "move_of_zero",
        "source_without_a_count",
        "negative_actual",
        "fractional_actual",
        "close_with_a_move",
        "own_bottles_with_a_count",
        "shared_place_with_an_account",
    ],
)
def test_bad_shape_is_400(db, admin_claim_headers, body):
    _, _, case = _jasur(db)

    assert _refused(case.id, body, admin_claim_headers) == INVALID
    _assert_still_open_without_claim(db, case.id)


def test_reason_is_trimmed_and_500_characters_are_accepted(db, admin_claim_headers):
    _, _, case = _jasur(db)
    reason = "x" * 500

    detail = _resolved(case.id, {"kind": "close", "reason": f"  {reason}  "}, admin_claim_headers)

    assert detail["resolution"]["reason"] == reason


def test_missing_case_is_404(db, admin_claim_headers):
    assert _refused(424242, {"kind": "close", "reason": "r"}, admin_claim_headers) == (
        404,
        "SHARED_PLACE_CASE_NOT_FOUND",
    )


def test_manager_gets_403(db, admin_claim_headers, manager_claim_headers):
    _, _, case = _jasur(db)

    response = _resolve(case.id, {"kind": "close", "reason": "r"}, manager_claim_headers)

    assert response.status_code == 403
    _assert_still_open_without_claim(db, case.id)
    assert _resolved(case.id, {"kind": "close", "reason": "r"}, admin_claim_headers)["status"] == "closed"


def test_resolved_log_line(db, admin_claim_headers, caplog):
    _, akmal_addr = _neighbour(db)
    _, address, case = _jasur(db)
    group_id = _group(admin_claim_headers, [address.id, akmal_addr.id], "Office 12")

    body = {"kind": "shared_place", "reason": "coworkers"}
    with captured_logs(caplog, logging.getLogger(RESOLVE_LOGGER), logging.INFO):
        _resolved(case.id, body, admin_claim_headers)
        _resolved(case.id, body, admin_claim_headers)  # a replay logs nothing

    assert caplog.messages == [f"[SHARED_PLACE] case_resolved case={case.id} kind=shared_place group={group_id}"]


def test_close_logs_its_kind(db, admin_claim_headers, caplog):
    _, _, case = _jasur(db)

    with captured_logs(caplog, logging.getLogger(RESOLVE_LOGGER), logging.INFO):
        _resolved(case.id, {"kind": "close", "reason": "handled by phone"}, admin_claim_headers)

    assert caplog.messages == [f"[SHARED_PLACE] case_resolved case={case.id} kind=close group=None"]


# --------------------------------------------------------------------------------------------------------------------
# The whole journey, HTTP only
# --------------------------------------------------------------------------------------------------------------------


def test_full_journey_driver_answer_then_admin_resolves_shared_place(db, admin_claim_headers):
    _, akmal_addr = _neighbour(db)  # 8 empties, 14 m away
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=2)
    staff = current_app.test_client  # a fresh client per call: no cookie jar carries a token from one to the next

    # 1. The driver's Delivered tap, 3 empties typed.
    delivered = _driver_delivers(drv, delivery, 3)
    assert delivered.status_code == 200, delivered.get_json()

    # 2. The one question, the pick (p0) and the note.
    question = staff().get(QUESTION.format(delivery.id), headers=_bearer(drv)).get_json()["data"]
    assert (question["ask"], [c["first_name"] for c in question["candidates"]]) == (True, ["Akmal"])
    case_id = question["case_id"]
    picked = staff().put(ANSWER.format(case_id), json={"answer": "picked", "candidate_index": 0}, headers=_bearer(drv))
    assert picked.status_code == 200, picked.get_json()
    note = "Works with Akmal at the bakery"
    noted = staff().put(NOTE.format(case_id), json={"note": note}, headers=_bearer(drv))
    assert noted.status_code == 200, noted.get_json()

    # 3. The admin groups the two doors, the driver's note pre-filled as the place note.
    grouped = current_app.test_client().post(
        PLACE_GROUPS,
        json={"addressIds": [address.id, akmal_addr.id], "label": "Bakery", "reason": "coworkers", "notes": note},
        headers=admin_claim_headers,
    )
    assert grouped.status_code == 201, grouped.get_json()
    group_id = grouped.get_json()["data"]["place_group_id"]

    # 4. Resolve A.
    detail = _resolved(
        case_id,
        {"kind": "shared_place", "target_address_id": akmal_addr.id, "reason": "coworkers"},
        admin_claim_headers,
    )

    assert (detail["status"], detail["resolution"]["resulting_group_id"]) == ("resolved", group_id)
    assert (detail["answer"]["kind"], detail["answer"]["note"]) == ("picked", note)
    assert (detail["order"]["id"], detail["address"]["place_group"]) == (order.id, {"id": group_id, "label": "Bakery"})
    assert _pair(address.id) == (Decimal("7"), Decimal("7"))  # Akmal's 8 + 2 delivered - 3 back
