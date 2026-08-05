"""Customer bottle endpoints, place-group aware (plan 2c Task 5).

Covers:
- ``GET /api/v1/orders/bottles/my-balances`` -> cluster/place overview dict
  (``BottleTrackingService.get_customer_bottle_overview``).
- ``GET /api/v1/orders/bottles/my-ledger/<address_id>`` -> the place ledger,
  gated by ``CustomerLinkService.can_view_address_history``. A stranger now
  gets 404 instead of the old silent-empty-200; each legitimate arm of the
  three-arm gate still gets its data.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.customer_link import CanonicalCustomer
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType, UserRole, UserStatus, UserType

LAT, LNG = 41.3111, 69.2797


def _user(db, email, phone, first_name):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name=first_name, last_name="Member", user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, status=UserStatus.ACTIVE, is_verified=True,
             created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user, title="work"):
    a = UserAddress(user_id=user.id, title=title, full_address="Office",
                    latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _link(db, *users):
    """Put every user in one canonical cluster (primary = the first)."""
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# /bottles/my-balances — cluster + place overview
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_my_balances_overview_shows_place_balance_and_cluster_rows(client, app, db):
    """Renegotiated by the place re-key (was ``..._shows_place_union_and_cluster_rows``).

    Three assertions changed because the contract they pinned is gone:
      * ``cluster_total_balance == 6.0`` — a scalar cluster total is deliberately
        removed. Alice's place is SHARED with Bob and holds 5; attributing 2 of
        it to Alice would report the same bottles once per coworker. The rows
        below carry the same information without the double count.
      * ``place_union_balance`` -> ``place_balance``: there is no union left to
        derive, so it is read straight off the place's single row.
      * ``sum(m["balance"]) == headline`` — ``place_members`` is names-only
        (decision 4 removes the per-person slice everywhere). Replaced by the
        membership assertions, which are what that sum actually protected.
    The quantities (5 at the shared office, 4 at the sibling's own place) and
    the "Bob is not in Alice's cluster" property are unchanged.
    """
    admin = _user(db, "adm@example.com", "+998900000009", "Admin")
    alice = _user(db, "a@example.com", "+998900000001", "Alice")
    bob = _user(db, "b@example.com", "+998900000002", "Bob")
    a1, a2 = _address(db, alice), _address(db, bob)
    CustomerLinkService().create_place_group([a1.id, a2.id],
                                             acting_admin_id=admin.id, reason="office")
    # Alice also has a linked sibling account with its own address.
    sibling = _user(db, "a2@example.com", "+998900000003", "AliceTwo")
    _link(db, alice, sibling)
    a3 = _address(db, sibling, title="home")

    svc = BottleTrackingService()
    # One place, one seed (BOTTLE_INITIAL_BALANCE_EXISTS guard): Bob's 3 is a
    # second movement on the SAME pool, not a second seed of it. 2 + 3 = 5.
    svc.set_initial_balance(alice.id, a1.id, Decimal("2"), actor_user_id=admin.id)
    svc._create_ledger_entry(user_id=bob.id, address_id=a2.id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))
    svc.set_initial_balance(sibling.id, a3.id, Decimal("4"), actor_user_id=admin.id)

    resp = client.get("/api/v1/orders/bottles/my-balances", headers=_headers(app, alice))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["is_linked"] is True
    assert "cluster_total_balance" not in data  # summing a shared pool per member double-counts
    rows = {r["address_id"]: r for r in data["balances"]}
    assert set(rows) == {a1.id, a3.id}  # every cluster member's rows, not bob's
    grouped = rows[a1.id]
    assert grouped["is_grouped"] is True
    assert grouped["place_balance"] == 5.0
    names = {m["member_name"] for m in grouped["place_members"]}
    assert names == {"Alice Member", "Bob Member"}
    # The headline agrees with the SSOT helper...
    assert grouped["place_balance"] == float(BottleTrackingService.get_place_balance(a1.id))
    # ...and the member breakdown is complete, names-only, with the viewer flagged.
    assert [m["is_own"] for m in grouped["place_members"] if m["member_name"] == "Alice Member"] == [True]
    assert all(set(m) == {"member_name", "is_own"} for m in grouped["place_members"])
    sib_row = rows[a3.id]
    assert sib_row["is_own"] is False and sib_row["owner_name"] == "AliceTwo Member"
    assert sib_row["place_balance"] == 4.0


@pytest.mark.integration
def test_my_balances_unlinked_ungrouped_degrades_to_own_rows(client, app, db):
    """Regression baseline: a solo customer sees exactly their own rows, and
    every place field mirrors the address's own balance (ungrouped => the
    address IS the place).

    ``cluster_total_balance`` and ``balance`` are gone from the payload (see the
    sibling test's docstring); ``place_union_balance`` is now ``place_balance``.
    The 7 is unchanged."""
    admin = _user(db, "adm2@example.com", "+998900000019", "Admin")
    solo = _user(db, "solo@example.com", "+998900000011", "Solo")
    addr = _address(db, solo)
    BottleTrackingService().set_initial_balance(
        solo.id, addr.id, Decimal("7"), actor_user_id=admin.id
    )

    resp = client.get("/api/v1/orders/bottles/my-balances", headers=_headers(app, solo))
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["is_linked"] is False
    assert "cluster_total_balance" not in data
    assert len(data["balances"]) == 1
    row = data["balances"][0]
    assert row["address_id"] == addr.id
    # `balance` was the per-person figure; decision 4 removes it everywhere.
    # Pinned negatively so a regression re-introducing it cannot pass silently.
    assert "balance" not in row
    assert row["is_own"] is True
    assert row["is_grouped"] is False
    assert row["place_group_id"] is None
    assert row["place_balance"] == 7.0
    assert row["place_members"] == []
    assert row["owner_user_id"] == solo.id
    assert row["owner_name"] == "Solo Member"
    assert row["address_title"] == "work"
    assert row["full_address"] == "Office"


@pytest.mark.integration
def test_my_balances_orders_own_rows_first_then_siblings_by_balance(client, app, db):
    admin = _user(db, "adm3@example.com", "+998900000029", "Admin")
    alice = _user(db, "a3@example.com", "+998900000021", "Alice")
    sibling = _user(db, "a4@example.com", "+998900000022", "AliceTwo")
    _link(db, alice, sibling)
    own_small = _address(db, alice, title="own-small")
    own_big = _address(db, alice, title="own-big")
    sib = _address(db, sibling, title="sib")

    svc = BottleTrackingService()
    svc.set_initial_balance(alice.id, own_small.id, Decimal("1"), actor_user_id=admin.id)
    svc.set_initial_balance(alice.id, own_big.id, Decimal("5"), actor_user_id=admin.id)
    # Sibling's balance is the largest overall but must still sort AFTER own rows.
    svc.set_initial_balance(sibling.id, sib.id, Decimal("9"), actor_user_id=admin.id)

    resp = client.get("/api/v1/orders/bottles/my-balances", headers=_headers(app, alice))
    assert resp.status_code == 200, resp.get_json()
    rows = resp.get_json()["data"]["balances"]
    assert [r["address_id"] for r in rows] == [own_big.id, own_small.id, sib.id]
    assert [r["is_own"] for r in rows] == [True, True, False]


# --------------------------------------------------------------------------- #
# /bottles/my-ledger/<address_id> — three-arm gate + place ledger
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_my_ledger_gate_404_for_stranger_and_place_ledger_for_member(client, app, db):
    admin = _user(db, "adm@example.com", "+998900000009", "Admin")
    alice = _user(db, "a@example.com", "+998900000001", "Alice")
    bob = _user(db, "b@example.com", "+998900000002", "Bob")
    eve = _user(db, "e@example.com", "+998900000004", "Eve")
    a1, a2 = _address(db, alice), _address(db, bob)
    CustomerLinkService().create_place_group([a1.id, a2.id],
                                             acting_admin_id=admin.id, reason="office")
    svc = BottleTrackingService()
    # A place is one pool and can only be seeded once (BOTTLE_INITIAL_BALANCE_EXISTS
    # guard); bob's entry is a second movement on the same place, not a second seed.
    svc.set_initial_balance(alice.id, a1.id, Decimal("2"), actor_user_id=admin.id)
    svc._create_ledger_entry(user_id=bob.id, address_id=a2.id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))

    # Stranger: 404, no longer silent-empty-200.
    assert client.get(f"/api/v1/orders/bottles/my-ledger/{a1.id}",
                      headers=_headers(app, eve)).status_code == 404

    # Coworker (place-group arm): sees BOTH members' entries, redacted.
    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{a1.id}", headers=_headers(app, bob))
    assert resp.status_code == 200
    payload = resp.get_json()["data"]
    assert payload["total"] == 2
    for item in payload["items"]:
        assert "idempotency_key" not in item
        assert "entry_metadata" not in item
        assert "user_phone" not in item
        assert item["member_name"]


@pytest.mark.integration
def test_my_ledger_own_address_arm_still_returns_own_entries(client, app, db):
    """Arm 1 of the gate: the address owner keeps today's access."""
    admin = _user(db, "adm4@example.com", "+998900000039", "Admin")
    owner = _user(db, "own@example.com", "+998900000031", "Owner")
    addr = _address(db, owner)
    BottleTrackingService().set_initial_balance(
        owner.id, addr.id, Decimal("4"), actor_user_id=admin.id
    )

    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{addr.id}",
                      headers=_headers(app, owner))
    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()["data"]
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["address_id"] == addr.id
    assert item["event_type"] == "initial_balance"
    assert item["quantity"] == 4.0
    assert item["is_own"] is True
    assert item["member_name"] == "Owner Member"
    # Redaction holds on the ungrouped path too.
    assert "idempotency_key" not in item
    assert "entry_metadata" not in item
    assert "notes" not in item
    assert "actor_user_id" not in item


@pytest.mark.integration
def test_my_ledger_cluster_sibling_arm_returns_sibling_address_entries(client, app, db):
    """Arm 3 of the gate: the address owner is in the requester's cluster."""
    admin = _user(db, "adm5@example.com", "+998900000049", "Admin")
    alice = _user(db, "a5@example.com", "+998900000041", "Alice")
    sibling = _user(db, "a6@example.com", "+998900000042", "AliceTwo")
    _link(db, alice, sibling)
    sib_addr = _address(db, sibling)
    BottleTrackingService().set_initial_balance(
        sibling.id, sib_addr.id, Decimal("6"), actor_user_id=admin.id
    )

    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{sib_addr.id}",
                      headers=_headers(app, alice))
    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()["data"]
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["member_name"] == "AliceTwo Member"
    # Alice is a linked sibling, not the ledger row's user — not "own".
    assert item["is_own"] is False


@pytest.mark.integration
def test_my_ledger_unknown_address_is_404(client, app, db):
    stranger = _user(db, "nobody@example.com", "+998900000051", "Nobody")
    resp = client.get("/api/v1/orders/bottles/my-ledger/999999",
                      headers=_headers(app, stranger))
    assert resp.status_code == 404


@pytest.mark.integration
def test_my_ledger_pagination_passthrough(client, app, db):
    admin = _user(db, "adm6@example.com", "+998900000059", "Admin")
    alice = _user(db, "a7@example.com", "+998900000061", "Alice")
    bob = _user(db, "b7@example.com", "+998900000062", "Bob")
    a1, a2 = _address(db, alice), _address(db, bob)
    CustomerLinkService().create_place_group([a1.id, a2.id],
                                             acting_admin_id=admin.id, reason="office")
    svc = BottleTrackingService()
    # A place is one pool and can only be seeded once (BOTTLE_INITIAL_BALANCE_EXISTS
    # guard); bob's entry is a second movement on the same place, not a second seed.
    svc.set_initial_balance(alice.id, a1.id, Decimal("2"), actor_user_id=admin.id)
    svc._create_ledger_entry(user_id=bob.id, address_id=a2.id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"))

    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{a1.id}?page=2&per_page=1",
                      headers=_headers(app, alice))
    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()["data"]
    assert payload["page"] == 2
    assert payload["per_page"] == 1
    assert payload["total"] == 2
    assert len(payload["items"]) == 1
