"""Task 8 — the admin reads, serializers and routes are keyed by PLACE.

Three behaviours are pinned:

1. ``get_dashboard_stats`` counts PLACES, not people: two coworkers sharing one
   office are ONE debtor holding 7 bottles, never a 6/1 split across two rows.
2. ``serialize_bottle_balance`` exposes scope identity (``place_label`` +
   ``member_names``) instead of a single owner — a place row has none.
3. The admin ledger route is keyed by ``address_id`` alone; the place is
   resolved from it.
4. BLOCKER-1: an admin row action driven from the SERIALIZED row — never from a
   fixture id the UI does not have — reaches the right place.
"""

from decimal import Decimal

from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.serializers.bottle_serializers import serialize_bottle_balance
from shared.enums import BottleLedgerEventType


def test_dashboard_counts_places_not_people(app, db, place, sample_user, second_sample_user):
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("6"))
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("1"))
    db.session.flush()

    stats = svc.get_dashboard_stats()
    assert stats["total_bottles_out"] == 7.0
    assert stats["places_with_balance"] == 1
    assert len(stats["top_debtors"]) == 1
    assert stats["top_debtors"][0]["total_balance"] == 7.0
    assert stats["top_debtors"][0]["name"]      # never null — label or member names


def test_serializer_exposes_scope_identity_not_user(app, db, place, sample_user):
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"),
    )
    db.session.flush()
    row = BottleTrackingService.get_place_balance_row(place["a1"].id)
    data = serialize_bottle_balance(row)
    assert data["address_group_id"] == place["group"].id
    assert data["place_label"] == "office"
    assert data["member_names"]
    assert "user_id" not in data
    assert "user_name" not in data


def test_admin_ledger_route_is_address_keyed(app, client, admin_auth_headers, db, place, sample_user):
    BottleTrackingService()._create_ledger_entry(
        user_id=sample_user.id, address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("3"),
    )
    db.session.flush()
    resp = client.get(f"/api/v1/admin/bottles/ledger/{place['a1'].id}", headers=admin_auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["total"] == 1


def test_row_actions_drive_off_the_serialized_representative_address_id(
    app, client, admin_auth_headers, db, place, sample_user, second_sample_user
):
    """BLOCKER-1, covered the only way that proves anything.

    ``test_admin_ledger_route_is_address_keyed`` above passes a FIXTURE address
    id, which the admin table does not have: ``ck_bottle_balance_scope`` forces
    ``address_id IS NULL`` on a shared place, so the row an admin clicks carries
    no address id at all. The only id the Ledger / Reconcile / Adjust actions can
    send is ``representative_address_id`` — a field the serializer had to grow.
    Every id below therefore comes out of the API payload, never out of `place`:
    with a fixture id these routes pass while the real buttons 404.
    """
    svc = BottleTrackingService()
    svc._create_ledger_entry(user_id=sample_user.id, address_id=place["a1"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("5"))
    svc._create_ledger_entry(user_id=second_sample_user.id, address_id=place["a2"].id,
                             event_type=BottleLedgerEventType.DELIVERY, quantity=Decimal("2"))
    db.session.flush()

    listing = client.get("/api/v1/admin/bottles/balances", headers=admin_auth_headers)
    assert listing.status_code == 200
    rows = listing.get_json()["data"]["items"]
    row = next(r for r in rows if r["address_group_id"] == place["group"].id)

    # The row an admin sees: one shared pool, no address id, no owner.
    assert row["is_shared_place"] is True
    assert row["address_id"] is None
    assert row["balance"] == 7.0
    assert "user_id" not in row

    address_id = row["representative_address_id"]
    assert address_id is not None, "row actions have no id to send without this"
    assert address_id in row["member_address_ids"]

    # Row action: Ledger — sees the WHOLE place, both members' events.
    ledger = client.get(f"/api/v1/admin/bottles/ledger/{address_id}", headers=admin_auth_headers)
    assert ledger.status_code == 200
    assert ledger.get_json()["data"]["total"] == 2

    # Row action: Reconcile — one argument, and it lands on the same place.
    reconciled = client.post(
        f"/api/v1/admin/bottles/reconcile/{address_id}", headers=admin_auth_headers
    )
    assert reconciled.status_code == 200
    result = reconciled.get_json()["data"]
    assert result["address_group_id"] == place["group"].id
    assert result["address_id"] is None
    assert float(result["recalculated_balance"]) == 7.0
    assert float(result["discrepancy"]) == 0.0
