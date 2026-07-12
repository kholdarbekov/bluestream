"""Customer bottle ledger endpoint: GET /api/v1/orders/bottles/my-ledger/<address_id>.

Guards the additive ``order_number`` field, caller-scoping, auth, and the
eager-load N+1 fix in ``BottleTrackingService.get_address_ledger``.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import BottleLedger
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.password_security import hash_password
from shared.enums import BottleLedgerEventType


def _user(db, **kw):
    u = User(
        email=kw.pop("email", "ledger@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name="L",
        last_name="X",
        **kw,
    )
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user):
    # No lat/long => the UserAddress delivery-zone before_insert guard is skipped.
    a = UserAddress(user_id=user.id, full_address="1 Ledger St, Tashkent", is_default=True)
    db.session.add(a)
    db.session.commit()
    return a


def _order(db, user, address, number):
    o = Order(user_id=user.id, order_number=number, delivery_address_id=address.id)
    db.session.add(o)
    db.session.commit()
    return o


def _ledger_row(db, user, address, event_type, qty, balance, order=None):
    row = BottleLedger(
        user_id=user.id,
        address_id=address.id,
        order_id=order.id if order else None,
        event_type=event_type,
        quantity=Decimal(qty),
        balance_after=Decimal(balance),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _auth(user):
    return {
        "Authorization": "Bearer "
        + create_access_token(identity=str(user.id), additional_claims={"role": "customer"})
    }


@pytest.mark.integration
def test_my_ledger_order_number_present_and_null(app, db):
    user = _user(db, email="ledger1@example.com", telegram_id="tg-ledger1")
    address = _address(db, user)
    order = _order(db, user, address, "ORD-LINK-1")
    # Order-linked DELIVERY row.
    _ledger_row(db, user, address, BottleLedgerEventType.DELIVERY, "4", "4", order=order)
    # Standalone collection — no order.
    _ledger_row(db, user, address, BottleLedgerEventType.STANDALONE_COLLECTION, "-2", "2", order=None)

    client = app.test_client()
    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{address.id}", headers=_auth(user))

    assert resp.status_code == 200, resp.get_json()
    items = resp.get_json()["data"]["items"]
    assert len(items) == 2
    by_event = {i["event_type"]: i for i in items}
    assert by_event["delivery"]["order_number"] == "ORD-LINK-1"
    assert by_event["standalone_collection"]["order_number"] is None


@pytest.mark.integration
def test_my_ledger_scoped_to_caller(app, db):
    alice = _user(db, email="alice-ledger@example.com", telegram_id="tg-alice-l")
    bob = _user(db, email="bob-ledger@example.com", telegram_id="tg-bob-l")
    bob_addr = _address(db, bob)
    bob_order = _order(db, bob, bob_addr, "ORD-BOB-1")
    _ledger_row(db, bob, bob_addr, BottleLedgerEventType.DELIVERY, "3", "3", order=bob_order)

    # Alice probes Bob's address id — the (user_id, address_id) filter must
    # return nothing rather than leak Bob's ledger.
    client = app.test_client()
    resp = client.get(f"/api/v1/orders/bottles/my-ledger/{bob_addr.id}", headers=_auth(alice))

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["items"] == []


@pytest.mark.integration
def test_my_ledger_requires_auth(app, db):
    client = app.test_client()
    resp = client.get("/api/v1/orders/bottles/my-ledger/1")
    assert resp.status_code in (401, 422)


@pytest.mark.integration
def test_my_ledger_no_n_plus_one_on_order_number(app, db, count_queries):
    user = _user(db, email="ledger-nplus1@example.com", telegram_id="tg-nplus1")
    address = _address(db, user)
    for i in range(6):
        order = _order(db, user, address, f"ORD-NP-{i}")
        _ledger_row(db, user, address, BottleLedgerEventType.DELIVERY, "4", "4", order=order)

    user_id = user.id
    address_id = address.id
    # Detach every instance so the many-to-one ``order`` load cannot be served
    # from the identity map — a missing eager load then emits one SELECT per row.
    db.session.expunge_all()

    with count_queries() as counter:
        result = BottleTrackingService.get_address_ledger(user_id, address_id, per_page=20)

    assert sorted(r["order_number"] for r in result["items"]) == [f"ORD-NP-{i}" for i in range(6)]
    # Eager load => count() + one joined SELECT, independent of row count.
    # Lazy load => 2 + 6 = 8 queries. Bound cleanly separates the two.
    assert counter.count <= 4, "N+1 detected: {} queries\n{}".format(
        counter.count, "\n".join(counter.statements)
    )
