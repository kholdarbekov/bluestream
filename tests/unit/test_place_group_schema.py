"""Schema round-trips for Phase 2a: ownerless place groups, allocation-scope
columns, dual audit stamps, and the place-suggestion dismiss registry.

SQLite runs with FKs OFF — FK integrity is verified against dev Postgres in the
migration step of this task, not here.
"""
from datetime import datetime, UTC
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.customer_link import AddressGroup, PlaceSuggestionDismissal
from business_app.models.payment import CashCollectionAllocation, CashCollectionEvent
from shared.enums import CashCollectionSource


@pytest.mark.unit
class TestPlaceGroupSchema:
    def test_address_group_persists_without_canonical(self, db):
        group = AddressGroup(canonical_customer_id=None, label="office")
        db.session.add(group)
        db.session.commit()
        assert group.id is not None
        assert AddressGroup.query.get(group.id).canonical_customer_id is None

    def test_place_suggestion_dismissal_round_trip(self, db):
        row = PlaceSuggestionDismissal(
            address_id_low=11, address_id_high=22,
            dismissed_by_admin_id=None, signal_fingerprint="abc123",
        )
        db.session.add(row)
        db.session.commit()
        got = PlaceSuggestionDismissal.query.one()
        assert (got.address_id_low, got.address_id_high) == (11, 22)
        assert got.signal_fingerprint == "abc123"
        assert got.created_at is not None

    def test_place_suggestion_dismissal_pair_is_unique(self, db):
        db.session.add(PlaceSuggestionDismissal(address_id_low=11, address_id_high=22))
        db.session.commit()
        db.session.add(PlaceSuggestionDismissal(address_id_low=11, address_id_high=22))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_cash_event_scope_defaults_to_personal(self, db):
        event = CashCollectionEvent(
            customer_id=1,
            amount=Decimal("10000.00"),
            currency="UZS",
            source=CashCollectionSource.ADMIN_ADJUSTMENT,
            occurred_at=datetime.now(UTC),
            unapplied_amount=Decimal("10000.00"),
        )
        db.session.add(event)
        db.session.commit()
        got = CashCollectionEvent.query.get(event.id)
        assert got.scope_type == "personal"
        assert got.scope_snapshot is None

    def test_cash_event_stores_place_snapshot(self, db):
        snapshot = {
            "group_id": 7,
            "address_ids": [1, 2],
            "place_user_ids": [10, 20],
            "orderer_cluster_user_ids": [10],
        }
        event = CashCollectionEvent(
            customer_id=10,
            amount=Decimal("5000.00"),
            currency="UZS",
            source=CashCollectionSource.DELIVERY_COMPLETION,
            occurred_at=datetime.now(UTC),
            unapplied_amount=Decimal("0.00"),
            scope_type="place",
            scope_snapshot=snapshot,
        )
        db.session.add(event)
        db.session.commit()
        assert CashCollectionEvent.query.get(event.id).scope_snapshot == snapshot

    def test_allocation_stamps_default_null_and_round_trip(self, db):
        event = CashCollectionEvent(
            customer_id=1,
            amount=Decimal("10000.00"),
            currency="UZS",
            source=CashCollectionSource.ADMIN_ADJUSTMENT,
            occurred_at=datetime.now(UTC),
            unapplied_amount=Decimal("10000.00"),
        )
        db.session.add(event)
        db.session.flush()
        bare = CashCollectionAllocation(
            cash_collection_event_id=event.id, payment_id=1,
            allocated_amount=Decimal("1000.00"), allocation_order=1,
        )
        stamped = CashCollectionAllocation(
            cash_collection_event_id=event.id, payment_id=2,
            allocated_amount=Decimal("2000.00"), allocation_order=2,
            source_customer_id=1, beneficiary_user_id=99,
        )
        db.session.add_all([bare, stamped])
        db.session.commit()
        assert bare.source_customer_id is None and bare.beneficiary_user_id is None
        assert stamped.source_customer_id == 1 and stamped.beneficiary_user_id == 99
