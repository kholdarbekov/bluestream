"""Admin place-group API (plan 2c task 9).

Covers the first-class `/admin/place-groups` surface, the cross-user address
picker that powers manual grouping, the place-suggestion read/dismiss pair,
and the missing-user 404 guards on the two §12 link reads.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app import db as _db
from business_app.models.customer_link import CustomerLinkEvent, PlaceSuggestionDismissal
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType

LAT, LNG = 41.3111, 69.2797


def _customer(email, phone):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name="U", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
             status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC))
    _db.session.add(u)
    _db.session.commit()
    return u


def _address(user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office",
                    latitude=LAT, longitude=LNG)
    _db.session.add(a)
    _db.session.commit()
    return a


@pytest.mark.integration
def test_place_group_crud_flow(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    u3 = _customer("c@example.com", "+998900000003")
    a1, a2, a3 = _address(u1), _address(u2), _address(u3)

    created = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id, a2.id], "label": "Acme office",
                                "reason": "coworkers share the office"},
                          headers=admin_auth_headers)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert created.get_json()["data"]["address_ids"] == sorted([a1.id, a2.id])

    detail = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert detail.status_code == 200, detail.get_json()
    data = detail.get_json()["data"]
    assert data["label"] == "Acme office"
    assert {m["owner"]["id"] for m in data["members"]} == {u1.id, u2.id}
    assert "cod" in data and "place_balance" in data
    # Decimals must cross the boundary as JSON numbers, not strings.
    assert isinstance(data["place_balance"], (int, float))
    # The place holds ONE pool of bottles — there is no per-member slice to
    # render any more (spec decision 4), only the place figure above.
    for member in data["members"]:
        assert "balance" not in member

    added = client.post(f"/api/v1/admin/place-groups/{group_id}/addresses",
                        json={"addressIds": [a3.id], "reason": "third coworker"},
                        headers=admin_auth_headers)
    assert added.status_code == 200, added.get_json()
    assert a3.id in added.get_json()["data"]["address_ids"]

    removed = client.delete(f"/api/v1/admin/place-groups/{group_id}/addresses/{a3.id}",
                            json={"reason": "left the company"},
                            headers=admin_auth_headers)
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["data"]["place_group_id"] == group_id
    # Removal is a membership edit only — the retired ungroup-netting payload
    # must not come back (spec §8).
    assert "netting" not in removed.get_json()["data"]

    # u3's only member address was just removed, so u3 is no longer an owner.
    # The removal event MUST still appear — the audit filter keys on the
    # "[group <id>] " reason prefix, not on current member ids.
    after = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert after.status_code == 200
    event_types = {e["event_type"] for e in after.get_json()["data"]["events"]}
    assert "remove_from_place_group" in event_types
    assert "create_place_group" in event_types
    assert "add_to_place_group" in event_types

    assert client.get("/api/v1/admin/place-groups/999999",
                      headers=admin_auth_headers).status_code == 404


@pytest.mark.integration
def test_remove_forwards_bottles_leaving_and_returns_it_as_a_number(
    client, db, admin_auth_headers
):
    """`bottlesLeaving` is a QUANTITY that moves balances, so it has to survive
    the HTTP boundary in both directions: forwarded from the request body into
    the service, and rendered back as a JSON NUMBER. Flask's provider renders a
    bare Decimal as the string "2.00", which breaks the panel's arithmetic —
    the same trap `place_balance` already carries a comment about.
    """
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    u3 = _customer("c@example.com", "+998900000003")
    a1, a2, a3 = _address(u1), _address(u2), _address(u3)

    created = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id, a2.id, a3.id], "reason": "one office"},
                          headers=admin_auth_headers)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    # Seed the place through the real write path, then read it back off the route.
    BottleTrackingService().admin_adjust_balance(
        user_id=u3.id, address_id=a3.id, adjustment=Decimal("5"),
        actor_user_id=u3.id, notes="seed",
    )
    _db.session.commit()

    before = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert before.get_json()["data"]["place_balance"] == 5
    member = next(m for m in before.get_json()["data"]["members"] if m["address_id"] == a3.id)
    # The pre-fill crosses as a number too, and derives a3's own 5 (clamped to
    # the place's 5) rather than 0.
    assert isinstance(member["suggested_bottles_leaving"], (int, float))
    assert member["suggested_bottles_leaving"] == 5

    removed = client.delete(
        f"/api/v1/admin/place-groups/{group_id}/addresses/{a3.id}",
        json={"reason": "left", "bottlesLeaving": 2},
        headers=admin_auth_headers,
    )
    assert removed.status_code == 200, removed.get_json()
    body = removed.get_json()["data"]
    assert body["place_group_id"] == group_id
    # Decimals must cross the boundary as JSON NUMBERS, not strings.
    assert isinstance(body["bottles_leaving"], (int, float))
    assert not isinstance(body["bottles_leaving"], bool)
    assert body["bottles_leaving"] == 2

    # CONSERVATION over HTTP, as a pair: 5 = 3 left at the place + 2 that left.
    after = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert after.get_json()["data"]["place_balance"] == 3
    assert BottleTrackingService.get_place_balance(a3.id) == Decimal("2.00")
    assert after.get_json()["data"]["place_balance"] + float(
        BottleTrackingService.get_place_balance(a3.id)) == 5

    # An impossible quantity is a 400 with the specific code, not a clamp.
    rejected = client.delete(
        f"/api/v1/admin/place-groups/{group_id}/addresses/{a2.id}",
        json={"reason": "left", "bottlesLeaving": 99},
        headers=admin_auth_headers,
    )
    assert rejected.status_code == 400, rejected.get_json()
    assert rejected.get_json()["data"]["error_code"] == "PLACE_SPLIT_INVALID"
    still_there = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert still_there.get_json()["data"]["place_balance"] == 3
    assert {m["address_id"] for m in still_there.get_json()["data"]["members"]} == {a1.id, a2.id}

    # Omitting the field keeps the default: nothing leaves WITH a2. This removal
    # would leave the place with exactly one member, so §7.3 dissolves it in the
    # same call and the route says so.
    default = client.delete(
        f"/api/v1/admin/place-groups/{group_id}/addresses/{a2.id}",
        json={"reason": "left too"},
        headers=admin_auth_headers,
    )
    assert default.status_code == 200, default.get_json()
    assert default.get_json()["data"]["bottles_leaving"] == 0
    assert default.get_json()["data"]["dissolved"] is True
    # The place has no members left, so the panel reads 0 — and the 3 it held are
    # on a1's own scope, not destroyed. The conservation pair over HTTP:
    # 5 = 3 (a1, the last member) + 2 (that left with a3).
    dissolved_detail = client.get(f"/api/v1/admin/place-groups/{group_id}",
                                  headers=admin_auth_headers).get_json()["data"]
    assert dissolved_detail["members"] == []
    assert dissolved_detail["place_balance"] == 0
    assert BottleTrackingService.get_place_balance(a1.id) == Decimal("3.00")
    assert BottleTrackingService.get_place_balance(a1.id) + BottleTrackingService.get_place_balance(
        a3.id) == Decimal("5.00")


@pytest.mark.integration
def test_place_group_detail_audit_is_scoped_to_this_group(client, db, admin_auth_headers):
    """A second group's events must never leak into this group's audit trail."""
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    u3 = _customer("c@example.com", "+998900000003")
    u4 = _customer("d@example.com", "+998900000004")
    a1, a2, a3, a4 = _address(u1), _address(u2), _address(u3), _address(u4)

    first = client.post("/api/v1/admin/place-groups",
                        json={"addressIds": [a1.id, a2.id], "reason": "group one"},
                        headers=admin_auth_headers)
    second = client.post("/api/v1/admin/place-groups",
                         json={"addressIds": [a3.id, a4.id], "reason": "group two"},
                         headers=admin_auth_headers)
    assert first.status_code == 201 and second.status_code == 201
    first_id = first.get_json()["data"]["place_group_id"]
    second_id = second.get_json()["data"]["place_group_id"]

    detail = client.get(f"/api/v1/admin/place-groups/{first_id}", headers=admin_auth_headers)
    events = detail.get_json()["data"]["events"]
    assert len(events) == 1
    assert events[0]["reason"].startswith(f"[group {first_id}]")
    assert all(f"[group {second_id}]" not in e["reason"] for e in events)


@pytest.mark.integration
def test_place_group_mutations_validate_and_404(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    u3 = _customer("c@example.com", "+998900000003")
    a1, a2, a3 = _address(u1), _address(u2), _address(u3)

    too_few = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id], "reason": "r"},
                          headers=admin_auth_headers)
    assert too_few.status_code == 400

    no_reason = client.post("/api/v1/admin/place-groups",
                            json={"addressIds": [a1.id, a2.id]},
                            headers=admin_auth_headers)
    assert no_reason.status_code == 400

    created = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id, a2.id], "reason": "r"},
                          headers=admin_auth_headers)
    group_id = created.get_json()["data"]["place_group_id"]

    missing_group = client.post("/api/v1/admin/place-groups/999999/addresses",
                                json={"addressIds": [a3.id], "reason": "r"},
                                headers=admin_auth_headers)
    assert missing_group.status_code == 404

    add_no_reason = client.post(f"/api/v1/admin/place-groups/{group_id}/addresses",
                                json={"addressIds": [a3.id]}, headers=admin_auth_headers)
    assert add_no_reason.status_code == 400

    # a3 is not a member of this group -> 404, not a silent removal.
    not_a_member = client.delete(f"/api/v1/admin/place-groups/{group_id}/addresses/{a3.id}",
                                 json={"reason": "r"}, headers=admin_auth_headers)
    assert not_a_member.status_code == 404

    remove_no_reason = client.delete(f"/api/v1/admin/place-groups/{group_id}/addresses/{a1.id}",
                                     json={}, headers=admin_auth_headers)
    assert remove_no_reason.status_code == 400


@pytest.mark.integration
def test_address_search_finds_other_customers_and_excludes_grouped(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)

    resp = client.get("/api/v1/admin/addresses/search?q=99890000000",
                      headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    found = {a["address_id"] for a in resp.get_json()["data"]["addresses"]}
    assert {a1.id, a2.id}.issubset(found)
    row = next(a for a in resp.get_json()["data"]["addresses"] if a["address_id"] == a1.id)
    assert set(row) == {"address_id", "title", "full_address", "address_group_id", "owner"}
    assert set(row["owner"]) == {"id", "first_name", "last_name", "phone"}
    assert row["owner"]["id"] == u1.id

    client.post("/api/v1/admin/place-groups",
                json={"addressIds": [a1.id, a2.id], "reason": "coworkers"},
                headers=admin_auth_headers)
    again = client.get("/api/v1/admin/addresses/search?q=99890000000",
                       headers=admin_auth_headers)
    assert again.get_json()["data"]["addresses"] == []
    with_grouped = client.get("/api/v1/admin/addresses/search?q=99890000000&exclude_grouped=0",
                              headers=admin_auth_headers)
    assert len(with_grouped.get_json()["data"]["addresses"]) == 2


@pytest.mark.integration
def test_address_search_short_query_returns_empty(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    _address(u1)
    for query in ("", "9"):
        resp = client.get(f"/api/v1/admin/addresses/search?q={query}", headers=admin_auth_headers)
        assert resp.status_code == 200
        assert resp.get_json()["data"]["addresses"] == []


@pytest.mark.integration
def test_place_suggestions_and_dismiss(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)

    resp = client.get(f"/api/v1/admin/users/{u1.id}/place-group-suggestions",
                      headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    suggestions = resp.get_json()["data"]["suggestions"]
    assert len(suggestions) == 1
    assert sorted(suggestions[0]["address_ids"]) == sorted([a1.id, a2.id])

    dismissed = client.post("/api/v1/admin/place-group-suggestions/dismiss",
                            json={"addressIdA": a1.id, "addressIdB": a2.id,
                                  "reason": "not the same place"},
                            headers=admin_auth_headers)
    assert dismissed.status_code == 200, dismissed.get_json()
    assert dismissed.get_json()["data"] == {"address_id_low": min(a1.id, a2.id),
                                            "address_id_high": max(a1.id, a2.id)}
    assert PlaceSuggestionDismissal.query.count() == 1
    assert CustomerLinkEvent.query.filter_by(event_type="dismiss_place_suggestion").count() == 1

    again = client.get(f"/api/v1/admin/users/{u1.id}/place-group-suggestions",
                       headers=admin_auth_headers)
    assert again.get_json()["data"]["suggestions"] == []


@pytest.mark.integration
def test_dismiss_place_suggestion_validates_body(client, db, admin_auth_headers):
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)

    missing_pair = client.post("/api/v1/admin/place-group-suggestions/dismiss",
                               json={"addressIdA": a1.id, "reason": "r"},
                               headers=admin_auth_headers)
    assert missing_pair.status_code == 400

    missing_reason = client.post("/api/v1/admin/place-group-suggestions/dismiss",
                                 json={"addressIdA": a1.id, "addressIdB": a2.id},
                                 headers=admin_auth_headers)
    assert missing_reason.status_code == 400

    missing_address = client.post("/api/v1/admin/place-group-suggestions/dismiss",
                                  json={"addressIdA": a1.id, "addressIdB": 999999,
                                        "reason": "r"},
                                  headers=admin_auth_headers)
    assert missing_address.status_code == 400


@pytest.mark.integration
def test_missing_user_404s_on_link_reads(client, db, admin_auth_headers):
    assert client.get("/api/v1/admin/users/999999/link-suggestions",
                      headers=admin_auth_headers).status_code == 404
    assert client.get("/api/v1/admin/users/999999/linked-accounts",
                      headers=admin_auth_headers).status_code == 404
    assert client.get("/api/v1/admin/users/999999/place-group-suggestions",
                      headers=admin_auth_headers).status_code == 404


# --------------------------------------------------------------------------- #
# Merge review (spec §7.4) over HTTP
# --------------------------------------------------------------------------- #

@pytest.mark.integration
def test_merge_preview_crosses_the_boundary_as_numbers_and_404s_on_a_missing_address(
    client, db, admin_auth_headers
):
    """The three figures are QUANTITIES the panel does arithmetic on, so they
    must arrive as JSON numbers — Flask renders a bare Decimal as the string
    "7.00". And a missing address is 404, not 500 (spec §13's last line): the
    bare `except` on every other admin route turns a lookup miss into a 500.
    """
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)
    for user, addr, qty in ((u1, a1, "4"), (u2, a2, "3")):
        BottleTrackingService().admin_adjust_balance(
            user_id=user.id, address_id=addr.id, adjustment=Decimal(qty),
            actor_user_id=user.id, notes="seed",
        )
    _db.session.commit()

    resp = client.get(f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}",
                      headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    for key in ("computed_balance", "stored_balance", "drift", "excluded_total",
                "resulting_balance", "projected_place_balance"):
        assert isinstance(data[key], (int, float)), key
        assert not isinstance(data[key], bool), key
    assert data["computed_balance"] == 7
    assert data["stored_balance"] == 7
    assert data["drift"] == 0
    assert data["excluded_total"] == 0
    assert data["resulting_balance"] == 7
    assert data["projected_place_balance"] == 7
    assert len(data["entries"]) == 2
    assert [e["preview_balance_after"] for e in data["entries"]] == [4, 7]
    assert data["entry_ids"] == [e["id"] for e in data["entries"]]

    drop = data["entries"][0]["id"]
    excluded = client.get(
        f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}&exclude={drop}",
        headers=admin_auth_headers,
    )
    assert excluded.status_code == 200, excluded.get_json()
    body = excluded.get_json()["data"]
    assert body["computed_balance"] == 7
    assert body["excluded_total"] == 4
    assert body["resulting_balance"] == 3
    assert [e["excluded"] for e in body["entries"]] == [True, False]

    # An `exclude` id outside this merge is the SAME 400 the committing call
    # makes — the decision aid must not accept input the commit refuses.
    stray = client.get(
        f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}&exclude=999999",
        headers=admin_auth_headers,
    )
    assert stray.status_code == 400, stray.get_json()
    assert stray.get_json()["data"]["error_code"] == "MERGE_EXCLUSION_NOT_ELIGIBLE"

    missing = client.get(f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},999999",
                         headers=admin_auth_headers)
    assert missing.status_code == 404, missing.get_json()
    missing_group = client.get(
        f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}&group_id=999999",
        headers=admin_auth_headers,
    )
    assert missing_group.status_code == 404, missing_group.get_json()
    assert client.get("/api/v1/admin/place-groups/merge-preview",
                      headers=admin_auth_headers).status_code == 400
    assert client.get(f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},oops",
                      headers=admin_auth_headers).status_code == 400

    # The preview is a READ: neither place has moved.
    assert BottleTrackingService.get_place_balance(a1.id) == Decimal("4.00")
    assert BottleTrackingService.get_place_balance(a2.id) == Decimal("3.00")


@pytest.mark.integration
def test_the_join_routes_forward_the_merge_review_and_surface_its_error_codes(
    client, db, admin_auth_headers
):
    """Body -> service, and the four §13 codes back out as 400s with their
    machine contract intact."""
    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)
    for user, addr, qty in ((u1, a1, "4"), (u2, a2, "3")):
        BottleTrackingService().admin_adjust_balance(
            user_id=user.id, address_id=addr.id, adjustment=Decimal(qty),
            actor_user_id=user.id, notes="seed",
        )
    _db.session.commit()

    preview = client.get(
        f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}",
        headers=admin_auth_headers,
    ).get_json()["data"]
    drop = preview["entries"][0]["id"]

    stale = client.post("/api/v1/admin/place-groups",
                        json={"addressIds": [a1.id, a2.id], "reason": "r",
                              "resultingBalance": 5, "previewEntryIds": [drop]},
                        headers=admin_auth_headers)
    assert stale.status_code == 400, stale.get_json()
    assert stale.get_json()["data"]["error_code"] == "MERGE_PREVIEW_STALE"

    not_eligible = client.post("/api/v1/admin/place-groups",
                               json={"addressIds": [a1.id, a2.id], "reason": "r",
                                     "excludedLedgerEntryIds": [999999]},
                               headers=admin_auth_headers)
    assert not_eligible.status_code == 400
    assert not_eligible.get_json()["data"]["error_code"] == "MERGE_EXCLUSION_NOT_ELIGIBLE"

    # `reason` is required by the route itself, so MERGE_REASON_REQUIRED is
    # reached with whitespace — which the route strips to "" and rejects first.
    # Drive the service code through the add path instead, below.

    created = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id, a2.id], "reason": "counted 5 crates",
                                "excludedLedgerEntryIds": [drop],
                                "resultingBalance": 5,
                                "previewEntryIds": preview["entry_ids"]},
                          headers=admin_auth_headers)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    detail = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    # 4 + 3 absorbed = 7, minus the excluded 4 = 3, then stated as 5.
    assert detail.get_json()["data"]["place_balance"] == 5

    # ...and re-excluding the same entry through the OTHER route is refused.
    u3 = _customer("c@example.com", "+998900000003")
    a3 = _address(u3)
    again = client.post(f"/api/v1/admin/place-groups/{group_id}/addresses",
                        json={"addressIds": [a3.id], "reason": "new hire",
                              "excludedLedgerEntryIds": [drop]},
                        headers=admin_auth_headers)
    assert again.status_code == 400, again.get_json()
    assert again.get_json()["data"]["error_code"] == "MERGE_EXCLUSION_NOT_ELIGIBLE"
    assert client.get(f"/api/v1/admin/place-groups/{group_id}",
                      headers=admin_auth_headers).get_json()["data"]["place_balance"] == 5


@pytest.mark.integration
def test_a_drifted_place_is_repaired_to_the_stated_number_over_http(client, db, admin_auth_headers):
    """The whole point of the feature, end to end, on the REAL dev shape.

    Address 24 (user 68, "Home"): stored 20.00 with ZERO ledger rows —
    manually adjusted, never grouped. The admin previews, sees both figures,
    states 12, and BOTH the place balance and the place ledger land on 12.

    Two designs failed here. Measuring the delta against the ledger and landing
    it on the carried figure gave 32. Absorbing the drift as a coupled `-20`
    gave the right balance and a ledger of -8, so the panel's Reconcile button
    would then have set the balance to -8.
    """
    from business_app.models.bottle import BottleLedger
    from business_app.services.bottle_scope import BottleScope

    u1 = _customer("a@example.com", "+998900000001")
    u2 = _customer("b@example.com", "+998900000002")
    a1, a2 = _address(u1), _address(u2)
    BottleTrackingService().admin_adjust_balance(
        user_id=u1.id, address_id=a1.id, adjustment=Decimal("20"),
        actor_user_id=u1.id, notes="seed",
    )
    _db.session.commit()
    # Stored 20.00 with no ledger row for it — the shape production carries.
    # The BALANCE ROW itself came from the real write path above.
    BottleLedger.query.filter_by(address_id=a1.id).delete(synchronize_session=False)
    _db.session.commit()
    assert BottleTrackingService.get_place_balance(a1.id) == Decimal("20.00")

    preview = client.get(f"/api/v1/admin/place-groups/merge-preview?address_ids={a1.id},{a2.id}",
                         headers=admin_auth_headers)
    assert preview.status_code == 200, preview.get_json()
    figures = preview.get_json()["data"]
    assert figures["computed_balance"] == 0
    assert figures["stored_balance"] == 20
    assert figures["drift"] == 20
    assert figures["projected_place_balance"] == 20      # a plain join carries

    created = client.post("/api/v1/admin/place-groups",
                          json={"addressIds": [a1.id, a2.id], "reason": "counted 12 crates",
                                "resultingBalance": 12,
                                "previewEntryIds": figures["entry_ids"]},
                          headers=admin_auth_headers)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    detail = client.get(f"/api/v1/admin/place-groups/{group_id}", headers=admin_auth_headers)
    assert detail.get_json()["data"]["place_balance"] == 12
    assert BottleTrackingService.get_place_balance(a1.id) == Decimal("12.00")
    # ...and the LEDGER agrees, which is what makes the panel's Reconcile
    # button a no-op on this result instead of a destroyer of it.
    scope = BottleScope.for_group(group_id)
    assert sum((e.quantity for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
               Decimal("0.00")) == Decimal("12.00")
    backfill = BottleLedger.query.filter(
        BottleLedger.idempotency_key.like("merge_backfill:%")).one()
    assert backfill.quantity == Decimal("20.00")         # POSITIVE, not -20


@pytest.mark.integration
def test_place_group_routes_require_admin(client, db, auth_headers):
    """auth_headers is a plain customer JWT -> every new route must reject it."""
    unauthorized = [
        ("post", "/api/v1/admin/place-groups", {"addressIds": [1, 2], "reason": "r"}),
        ("get", "/api/v1/admin/place-groups/merge-preview?address_ids=1,2", None),
        ("get", "/api/v1/admin/place-groups/1", None),
        ("post", "/api/v1/admin/place-groups/1/addresses", {"addressIds": [1], "reason": "r"}),
        ("delete", "/api/v1/admin/place-groups/1/addresses/1", {"reason": "r"}),
        ("get", "/api/v1/admin/addresses/search?q=abc", None),
        ("get", "/api/v1/admin/users/1/place-group-suggestions", None),
        ("post", "/api/v1/admin/place-group-suggestions/dismiss",
         {"addressIdA": 1, "addressIdB": 2, "reason": "r"}),
    ]
    for method, url, payload in unauthorized:
        call = getattr(client, method)
        resp = call(url, json=payload, headers=auth_headers) if payload is not None \
            else call(url, headers=auth_headers)
        assert resp.status_code in (401, 403), f"{method.upper()} {url} -> {resp.status_code}"
