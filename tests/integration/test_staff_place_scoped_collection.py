"""Plan 2c / Task 3 — staff surfaces for place groups, end to end.

Three things are proven through the real HTTP surface:

  1. ``GET /staff/place-groups/<id>/cod-statement`` returns the unified place
     statement and 404s on an unknown group.
  2. A driver collection posted at a grouped address resolves PLACE scope and
     freezes it on the event — with ORDER context, and (the case Plan 2b's
     ``delivery_address_id`` parameter exists for) with NO order/delivery
     context at all, only the address. Without the endpoint forwarding
     ``delivery_address_id`` the second case silently degrades to
     personal/cluster scope and a coworker's debt at the same office is never
     settled, so the assertion is on the STORED ``scope_type``/``scope_snapshot``
     rather than merely on a 201.
  3. ``GET /staff/bottles/customer/<id>/addresses`` returns one row per PLACE
     carrying that place's whole pool, degrading to the address's own balance
     when ungrouped.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.payment import CashCollectionEvent
from business_app.models.user import User
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType, UserRole, UserType

# Reuse the user/address/debt/place builders defined in Task 2's test module
# (tests/ is an importable package — tests/__init__.py exists). Note
# _delivered_cod_debt returns (order, payment).
from tests.unit.test_place_cod_read_surfaces import _address, _delivered_cod_debt, _place, _user


@pytest.fixture
def staff_driver(db):
    user = User(
        email="place-driver@example.com",
        phone="+998900000055",
        password_hash="x",
        first_name="Place",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def staff_driver_auth_headers(app, staff_driver):
    with app.app_context():
        token = create_access_token(identity=str(staff_driver.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@pytest.mark.integration
def test_place_statement_route_and_place_scoped_post(client, db, staff_driver_auth_headers):
    admin = _user(db, "adm@example.com", "+998900000009")
    u1 = _user(db, "a@example.com", "+998900000001")
    u2 = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, u1), _address(db, u2)
    group = _place(db, [a1, a2], admin)
    order1, _ = _delivered_cod_debt(db, u1, "ORD-1", address=a1)
    _delivered_cod_debt(db, u2, "ORD-2", address=a2)

    resp = client.get(f"/api/v1/staff/place-groups/{group.id}/cod-statement",
                      headers=staff_driver_auth_headers)
    assert resp.status_code == 200
    stmt = resp.get_json()["data"]
    assert stmt["active_cod_debt_count"] == 2

    missing = client.get("/api/v1/staff/place-groups/999999/cod-statement",
                         headers=staff_driver_auth_headers)
    assert missing.status_code == 404

    # Standalone collection with ORDER context at the grouped address ->
    # the 2b engine resolves PLACE scope and stamps it on the event.
    post = client.post("/api/v1/staff/cash-collections",
                       json={"customer_id": u1.id, "amount": 30000.0,
                             "order_id": order1.id, "source": "standalone_meeting",
                             "notes": "collected at the office"},
                       headers=staff_driver_auth_headers)
    assert post.status_code == 201
    event_id = post.get_json()["data"]["cash_collection_event"]["id"]
    event = _db.session.get(CashCollectionEvent, event_id)
    assert event.scope_type == "place"
    assert event.scope_snapshot["group_id"] == group.id


@pytest.mark.integration
def test_orderless_standalone_collection_uses_delivery_address_id_for_place_scope(
    client, db, staff_driver_auth_headers
):
    """The pass-through under test: NO order_id, NO delivery_id — only the
    address. This is the only route by which an order-less standalone
    collection can reach PLACE scope."""
    admin = _user(db, "adm@example.com", "+998900000009")
    u1 = _user(db, "a@example.com", "+998900000001")
    u2 = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, u1), _address(db, u2)
    group = _place(db, [a1, a2], admin)
    _delivered_cod_debt(db, u1, "ORD-1", address=a1, outstanding=Decimal("15000.00"))
    _, coworker_payment = _delivered_cod_debt(db, u2, "ORD-2", address=a2,
                                              outstanding=Decimal("15000.00"))

    post = client.post("/api/v1/staff/cash-collections",
                       json={"customer_id": u1.id, "amount": 30000.0,
                             "delivery_address_id": a1.id,
                             "source": "standalone_meeting",
                             "notes": "met at the office door"},
                       headers=staff_driver_auth_headers)
    assert post.status_code == 201
    event = _db.session.get(CashCollectionEvent,
                            post.get_json()["data"]["cash_collection_event"]["id"])
    assert event.order_id is None and event.delivery_id is None
    assert event.scope_type == "place"
    assert event.scope_snapshot["group_id"] == group.id
    assert sorted(event.scope_snapshot["address_ids"]) == sorted([a1.id, a2.id])

    # The point of place scope: the coworker's debt at the same place is settled.
    _db.session.refresh(coworker_payment)
    assert Decimal(str(coworker_payment.outstanding_amount)) == Decimal("0.00")


@pytest.mark.integration
def test_orderless_standalone_collection_without_address_stays_unscoped(
    client, db, staff_driver_auth_headers
):
    """Regression baseline: omit delivery_address_id and nothing becomes
    place-scoped, so today's behaviour is untouched for callers that do not
    send the new field."""
    admin = _user(db, "adm@example.com", "+998900000009")
    u1 = _user(db, "a@example.com", "+998900000001")
    u2 = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, u1), _address(db, u2)
    _place(db, [a1, a2], admin)
    _delivered_cod_debt(db, u1, "ORD-1", address=a1, outstanding=Decimal("15000.00"))

    post = client.post("/api/v1/staff/cash-collections",
                       json={"customer_id": u1.id, "amount": 15000.0,
                             "source": "standalone_meeting", "notes": "no address given"},
                       headers=staff_driver_auth_headers)
    assert post.status_code == 201
    event = _db.session.get(CashCollectionEvent,
                            post.get_json()["data"]["cash_collection_event"]["id"])
    assert event.scope_type == "personal"


@pytest.mark.integration
def test_bottle_addresses_route_carries_place_fields(client, db, staff_driver_auth_headers):
    admin = _user(db, "adm@example.com", "+998900000009")
    u1 = _user(db, "a@example.com", "+998900000001")
    u2 = _user(db, "b@example.com", "+998900000002")
    a1, a2 = _address(db, u1), _address(db, u2)
    solo = _address(db, u1)  # u1's second, UNGROUPED address — a place of its own
    group = _place(db, [a1, a2], admin)

    svc = BottleTrackingService()
    # a1 and a2 are ONE place, so it can be seeded only once
    # (BOTTLE_INITIAL_BALANCE_EXISTS); the coworker's 3 is a movement on the
    # same pool, not a second seed. `solo` is a different place, so it seeds.
    svc.set_initial_balance(u1.id, a1.id, Decimal("2"), actor_user_id=admin.id)
    svc._create_ledger_entry(user_id=u2.id, address_id=a2.id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))
    svc.set_initial_balance(u1.id, solo.id, Decimal("4"), actor_user_id=admin.id)
    _db.session.commit()

    resp = client.get(f"/api/v1/staff/bottles/customer/{u1.id}/addresses",
                      headers=staff_driver_auth_headers)
    assert resp.status_code == 200
    rows = {r["address_id"]: r for r in resp.get_json()["data"]}
    # One row per PLACE — the shared office appears once, under u1's OWN address
    # there (a2 belongs to the coworker and must not leak into u1's payload).
    assert set(rows) == {a1.id, solo.id}

    grouped = rows[a1.id]
    assert grouped["is_grouped"] is True
    assert grouped["place_group_id"] == group.id
    assert grouped["place_balance"] == 5.0  # 2 + the coworker's 3, one pool
    # Decision 4: the pool has no per-person slice, and fines are address-keyed.
    assert "balance" not in grouped
    assert "bottle_balance_id" not in grouped

    ungrouped = rows[solo.id]
    assert ungrouped["is_grouped"] is False
    assert ungrouped["place_group_id"] is None
    assert ungrouped["place_balance"] == 4.0
