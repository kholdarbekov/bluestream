"""POST /api/v1/admin/bottles/reconcile/<address_id> — pins the re-keyed route.

``reconcile_bottle_balance`` (business_app/api/admin_bottles.py) calls
``BottleTrackingService.reconcile_balance(address_id)`` — the new place-scoped,
single-argument signature. Before this test existed nothing exercised the
route end-to-end, so the old two-positional-arg signature could regress here
without any test catching the resulting ``TypeError``.
"""

from decimal import Decimal

import pytest

from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from shared.enums import BottleLedgerEventType


@pytest.mark.integration
def test_reconcile_route_returns_200_and_reconciles_a_grouped_place(
    app, db, admin_auth_headers, place, sample_user, second_sample_user
):
    svc = BottleTrackingService()
    svc._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("4"),
    )
    svc._create_ledger_entry(
        user_id=second_sample_user.id, address_id=place["a2"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"),
    )
    db.session.commit()

    # Corrupt the materialized place balance so the route has something to fix.
    row = BottleBalance.query.filter_by(address_group_id=place["group"].id).one()
    row.balance = Decimal("999")
    db.session.commit()

    client = app.test_client()
    resp = client.post(
        f"/api/v1/admin/bottles/reconcile/{place['a1'].id}", headers=admin_auth_headers
    )

    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["address_group_id"] == place["group"].id
    assert data["recalculated_balance"] == 6.0
    assert data["corrected"] is True

    db.session.refresh(row)
    assert row.balance == Decimal("6.00")


@pytest.mark.integration
def test_reconcile_route_returns_200_for_an_ungrouped_address(
    app, db, admin_auth_headers, sample_user, user_address
):
    svc = BottleTrackingService()
    svc._create_ledger_entry(
        user_id=sample_user.id, address_id=user_address.id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"),
    )
    db.session.commit()

    client = app.test_client()
    resp = client.post(
        f"/api/v1/admin/bottles/reconcile/{user_address.id}", headers=admin_auth_headers
    )

    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["address_group_id"] is None
    assert data["address_id"] == user_address.id
    assert data["recalculated_balance"] == 3.0
    assert data["corrected"] is False


@pytest.mark.integration
def test_reconcile_route_requires_admin_auth(app, db, user_address):
    client = app.test_client()
    resp = client.post(f"/api/v1/admin/bottles/reconcile/{user_address.id}")
    assert resp.status_code in (401, 422)
