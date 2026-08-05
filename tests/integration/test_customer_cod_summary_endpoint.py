"""Customer wallet surface — ``GET /api/v1/payments/my-cod-summary`` (plan 2c Task 16).

Spec §7 promises the customer the MONEY half of the "one customer" picture that
tasks 4/5/10 already delivered for bottles:

* their whole linked CLUSTER's delivered-but-unpaid COD total and prepaid
  credit, shown as a single customer rather than per phone number, and
* for every grouped address (a shared workplace), that PLACE's unified open COD
  total together with a per-order breakdown naming the coworker each order
  belongs to — full in-group transparency is the approved decision.

Two invariants this file exists to pin, because both are silent when broken:

1. **Membership scoping.** Everything is derived from the AUTHENTICATED user —
   their own cluster, and only the place groups they (or a linked sibling)
   belong to. The route takes no client-supplied id, so a stranger's place or
   another person's cluster can never be addressed.
2. **Redaction.** Names cross the in-group boundary; phone numbers, payment /
   order / user ids and internal notes do not. ``get_place_cod_statement``
   returns all of those, so the route must project rather than pass through.

Regression baseline: an unlinked + ungrouped customer gets
``cluster_member_count == 1``, ``places == []`` and their own figures unchanged.
"""

import json
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from tests.unit.test_place_cod_read_surfaces import (
    _address,
    _delivered_cod_debt,
    _link,
    _place,
    _user,
)


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


def _iter_dicts(node):
    """Yield every dict nested anywhere inside a JSON-ish structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_dicts(value)


# --------------------------------------------------------------------------- #
# Place breakdown: names in, internals out
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_my_cod_summary_shows_place_breakdown_with_names_and_no_internals(client, app, db):
    admin = _user(db, "adm@example.com", "+998900000009")
    alice = _user(db, "a@example.com", "+998900000001")
    bob = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, alice), _address(db, bob)
    _place(db, [a1, a2], admin)
    _delivered_cod_debt(db, alice, "ORD-1", address=a1, outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, bob, "ORD-2", address=a2, outstanding=Decimal("20000.00"))

    resp = client.get("/api/v1/payments/my-cod-summary", headers=_headers(app, alice))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["cluster_member_count"] == 1
    assert data["cluster_delivered_outstanding_amount"] == 15000.0
    [place] = data["places"]
    assert place["place_open_cod_debt_total"] == 35000.0
    names = {i["member_name"] for i in place["items"]}
    assert names == {alice.full_name, bob.full_name}
    forbidden = {"phone", "payment_id", "order_id", "owner_user_id", "notes"}
    for item in place["items"]:
        assert forbidden.isdisjoint(item.keys())


@pytest.mark.integration
def test_my_cod_summary_never_leaks_a_coworkers_phone_or_internal_id(client, app, db):
    """Nothing anywhere in the payload — at any nesting depth — may carry a
    phone number or an internal id. Asserted over the serialized body, not the
    top level only, so a nested pass-through of the raw service dict fails."""
    admin = _user(db, "adm@example.com", "+998900000009")
    alice = _user(db, "a@example.com", "+998900000001")
    bob = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, alice), _address(db, bob)
    _place(db, [a1, a2], admin)
    _delivered_cod_debt(db, bob, "ORD-2", address=a2, outstanding=Decimal("20000.00"))
    bob_phone = bob.phone

    resp = client.get("/api/v1/payments/my-cod-summary", headers=_headers(app, alice))
    assert resp.status_code == 200
    data = resp.get_json()["data"]

    body = json.dumps(data)
    assert bob_phone not in body
    assert alice.phone not in body

    forbidden_keys = {
        "phone", "payment_id", "order_id", "owner_user_id", "user_id",
        "notes", "customer_id", "idempotency_key", "entry_metadata",
        "provider_data", "items_detail",
    }
    for node in _iter_dicts(data):
        assert forbidden_keys.isdisjoint(node.keys()), f"internal key leaked: {sorted(node.keys())}"

    # The coworker's name IS allowed (approved in-group transparency); the ids
    # behind it are not.
    [place] = data["places"]
    [item] = place["items"]
    assert item["member_name"] == bob.full_name
    assert set(item.keys()) == {"order_number", "member_name", "outstanding_amount", "created_at"}


# --------------------------------------------------------------------------- #
# Membership scoping — own cluster, own places, nothing else
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_my_cod_summary_excludes_other_peoples_places_and_clusters(client, app, db):
    """A customer sees ONLY the places they belong to and ONLY their own
    cluster's money — a stranger's grouped workplace and a stranger's debt are
    invisible, and there is no request parameter that could reach them."""
    admin = _user(db, "adm@example.com", "+998900000009")
    alice = _user(db, "a@example.com", "+998900000001")
    coworker = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, alice), _address(db, coworker)
    mine = _place(db, [a1, a2], admin, label="My office")
    _delivered_cod_debt(db, alice, "ORD-MINE", address=a1, outstanding=Decimal("15000.00"))

    # A completely separate office of two strangers, with its own debt.
    s1 = _user(db, "s1@example.com", "+998900000003")
    s2 = _user(db, "s2@example.com", "+998900000004")
    sa1, sa2 = _address(db, s1), _address(db, s2)
    theirs = _place(db, [sa1, sa2], admin, label="Their office")
    _delivered_cod_debt(db, s1, "ORD-THEIRS", address=sa1, outstanding=Decimal("99000.00"))

    data = client.get(
        "/api/v1/payments/my-cod-summary", headers=_headers(app, alice)
    ).get_json()["data"]

    assert [p["place_group_id"] for p in data["places"]] == [mine.id]
    assert theirs.id not in {p["place_group_id"] for p in data["places"]}
    body = json.dumps(data)
    assert "ORD-THEIRS" not in body
    assert "Their office" not in body
    # The stranger's 99000 never reaches either total.
    assert data["cluster_delivered_outstanding_amount"] == 15000.0
    assert data["places"][0]["place_open_cod_debt_total"] == 15000.0


@pytest.mark.integration
def test_my_cod_summary_cluster_total_spans_linked_accounts_only(client, app, db):
    """A linked person's two phone numbers report as ONE customer; an unlinked
    third party with debt of their own is excluded."""
    alice = _user(db, "a@example.com", "+998900000001")
    sibling = _user(db, "a2@example.com", "+998900000002")
    _link(db, [alice, sibling])
    stranger = _user(db, "x@example.com", "+998900000003")
    _delivered_cod_debt(db, alice, "ORD-1", outstanding=Decimal("15000.00"))
    _delivered_cod_debt(db, sibling, "ORD-2", outstanding=Decimal("25000.00"))
    _delivered_cod_debt(db, stranger, "ORD-X", outstanding=Decimal("99000.00"))

    data = client.get(
        "/api/v1/payments/my-cod-summary", headers=_headers(app, alice)
    ).get_json()["data"]

    assert data["cluster_member_count"] == 2
    assert data["cluster_delivered_outstanding_amount"] == 40000.0
    assert data["places"] == []


@pytest.mark.integration
def test_my_cod_summary_includes_a_linked_siblings_place(client, app, db):
    """The grouped address belongs to the SIBLING account; one person, one
    wallet, so the viewer must still see that place."""
    admin = _user(db, "adm@example.com", "+998900000009")
    alice = _user(db, "a@example.com", "+998900000001")
    sibling = _user(db, "a2@example.com", "+998900000002")
    _link(db, [alice, sibling])
    coworker = _user(db, "b@example.com", "+998900000003")
    sib_addr, co_addr = _address(db, sibling), _address(db, coworker)
    group = _place(db, [sib_addr, co_addr], admin, label="Sibling office")
    _delivered_cod_debt(db, coworker, "ORD-CO", address=co_addr, outstanding=Decimal("7000.00"))

    data = client.get(
        "/api/v1/payments/my-cod-summary", headers=_headers(app, alice)
    ).get_json()["data"]

    [place] = data["places"]
    assert place["place_group_id"] == group.id
    assert place["label"] == "Sibling office"
    assert place["place_open_cod_debt_total"] == 7000.0
    assert [i["member_name"] for i in place["items"]] == [coworker.full_name]


@pytest.mark.integration
def test_my_cod_summary_requires_authentication(client, app, db):
    assert client.get("/api/v1/payments/my-cod-summary").status_code == 401


# --------------------------------------------------------------------------- #
# Regression baseline — unlinked + ungrouped
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_my_cod_summary_unlinked_ungrouped_is_empty_places(client, app, db):
    solo = _user(db, "solo@example.com", "+998900000007")
    _delivered_cod_debt(db, solo, "ORD-9", address=_address(db, solo))
    resp = client.get("/api/v1/payments/my-cod-summary", headers=_headers(app, solo))
    data = resp.get_json()["data"]
    assert data["cluster_member_count"] == 1
    assert data["places"] == []


@pytest.mark.integration
def test_my_cod_summary_payload_shape_is_exactly_the_documented_contract(client, app, db):
    """Top-level keys are pinned: nothing from the (much larger) service
    statement may be added by accident."""
    solo = _user(db, "solo@example.com", "+998900000007")
    _delivered_cod_debt(db, solo, "ORD-9", address=_address(db, solo))
    data = client.get(
        "/api/v1/payments/my-cod-summary", headers=_headers(app, solo)
    ).get_json()["data"]

    assert set(data.keys()) == {
        "cluster_member_count",
        "cluster_delivered_outstanding_amount",
        "available_prepayment_balance",
        "places",
    }
    assert data["cluster_delivered_outstanding_amount"] == 15000.0
    assert data["available_prepayment_balance"] == 0.0
    assert isinstance(data["cluster_member_count"], int)
