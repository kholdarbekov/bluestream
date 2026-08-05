from decimal import Decimal

import pytest

from business_app.services.allocation_scope import AllocationScope
from business_app.services.cash_collection_service import CashCollectionService
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


@pytest.mark.unit
class TestResolveAllocationScope:
    def test_unlinked_ungrouped_is_personal(self, db):
        u = make_user(db)
        scope = CashCollectionService().resolve_allocation_scope(u.id)
        assert scope.scope_type == "personal"
        assert tuple(scope.orderer_cluster_user_ids) == (u.id,)
        assert scope.to_snapshot() is None

    def test_linked_customer_resolves_cluster(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        scope = CashCollectionService().resolve_allocation_scope(u1.id)
        assert scope.scope_type == "cluster"
        assert sorted(scope.orderer_cluster_user_ids) == sorted([u1.id, u2.id])

    def test_grouped_address_with_place_source_resolves_place(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = make_place_group(db, a1, a2)
        scope = CashCollectionService().resolve_allocation_scope(
            u1.id, delivery_address_id=a1.id, source="delivery_completion"
        )
        assert scope.scope_type == "place"
        assert scope.group_id == group.id
        assert sorted(scope.address_ids) == sorted([a1.id, a2.id])
        assert sorted(scope.place_user_ids) == sorted([u1.id, u2.id])
        assert tuple(scope.orderer_cluster_user_ids) == (u1.id,)

    def test_personal_card_transfer_never_place(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        scope = CashCollectionService().resolve_allocation_scope(
            u1.id, delivery_address_id=a1.id, source="personal_card_transfer"
        )
        assert scope.scope_type == "personal"  # unlinked payer -> personal

    def test_grocery_customer_forced_personal_even_if_linked_and_grouped(self, db):
        g = make_user(db, grocery=True)
        other = make_user(db)
        ag, ao = make_address(db, g), make_address(db, other)
        make_place_group(db, ag, ao)
        scope = CashCollectionService().resolve_allocation_scope(
            g.id, delivery_address_id=ag.id, source="delivery_completion"
        )
        assert scope.scope_type == "personal"

    def test_ungrouped_address_with_place_source_falls_back(self, db):
        u = make_user(db)
        a = make_address(db, u)
        scope = CashCollectionService().resolve_allocation_scope(
            u.id, delivery_address_id=a.id, source="delivery_completion"
        )
        assert scope.scope_type == "personal"

    def test_stranger_grouped_address_does_not_grant_place_scope(self, db):
        """Pin the circular-authorisation gate (spec 5.1).

        A grouped address the posting customer has NO membership in must never
        produce PLACE scope: otherwise the later scope-membership guard is
        circular — it would authorise settling strangers' debts purely because
        the caller pointed the collection at their grouped address.
        """
        stranger = make_user(db)
        member_a, member_b = make_user(db), make_user(db)
        addr_a, addr_b = make_address(db, member_a), make_address(db, member_b)
        make_place_group(db, addr_a, addr_b)
        scope = CashCollectionService().resolve_allocation_scope(
            stranger.id, delivery_address_id=addr_a.id, source="delivery_completion"
        )
        assert scope.scope_type == "personal"
        assert tuple(scope.orderer_cluster_user_ids) == (stranger.id,)
        assert scope.group_id is None
        assert scope.address_ids == ()

    def test_cluster_sibling_membership_grants_place_scope(self, db):
        """The gate accepts membership held by a cluster SIBLING, not just self."""
        payer, sibling = make_user(db), make_user(db)
        link_users(db, [payer, sibling])
        neighbour = make_user(db)
        sibling_addr = make_address(db, sibling)
        neighbour_addr = make_address(db, neighbour)
        group = make_place_group(db, sibling_addr, neighbour_addr)
        scope = CashCollectionService().resolve_allocation_scope(
            payer.id, delivery_address_id=sibling_addr.id, source="delivery_completion"
        )
        assert scope.scope_type == "place"
        assert scope.group_id == group.id
        assert sorted(scope.place_user_ids) == sorted([sibling.id, neighbour.id])
        assert sorted(scope.orderer_cluster_user_ids) == sorted([payer.id, sibling.id])


@pytest.mark.unit
class TestPostCollectionScopeStamping:
    def test_personal_post_stamps_personal_null_snapshot(self, db):
        u = make_user(db)
        order, _ = delivered_cod_order(db, u)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=u.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash meeting",
        )
        assert event.scope_type == "personal"
        assert event.scope_snapshot is None

    def test_place_post_freezes_four_field_snapshot(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = make_place_group(db, a1, a2)
        order, _ = delivered_cod_order(db, u1, address=a1)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash at office",
        )
        assert event.scope_type == "place"
        snap = event.scope_snapshot
        assert snap["group_id"] == group.id
        assert sorted(snap["address_ids"]) == sorted([a1.id, a2.id])
        assert sorted(snap["place_user_ids"]) == sorted([u1.id, u2.id])
        assert snap["orderer_cluster_user_ids"] == [u1.id]
        # Round-trip through from_event
        restored = AllocationScope.from_event(event)
        assert restored.scope_type == "place"
        assert sorted(restored.address_ids) == sorted([a1.id, a2.id])

    def test_post_against_stranger_place_address_stamps_personal(self, db):
        """End-to-end pin of the circular-authorisation gate on post_collection.

        The customer's own order is delivered to an address that belongs to a
        place group the customer is not part of. The event must stay PERSONAL.
        """
        payer = make_user(db)
        member_a, member_b = make_user(db), make_user(db)
        addr_a, addr_b = make_address(db, member_a), make_address(db, member_b)
        make_place_group(db, addr_a, addr_b)
        order, _ = delivered_cod_order(db, payer, address=addr_a)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=payer.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash at stranger door",
        )
        assert event.scope_type == "personal"
        assert event.scope_snapshot is None

    def test_idempotent_replay_returns_stored_event_with_stored_scope(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        order, _ = delivered_cod_order(db, u1, address=a1)
        admin = make_user(db)
        svc = CashCollectionService()
        kwargs = dict(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cash at office",
            idempotency_key="scope-replay-1",
        )
        first = svc.post_collection(**kwargs)
        # Topology change between post and replay: ungroup a2.
        a2.address_group_id = None
        db.session.commit()
        replay = svc.post_collection(**kwargs)
        assert replay.id == first.id
        assert replay.scope_type == "place"
        assert sorted(replay.scope_snapshot["address_ids"]) == sorted([a1.id, a2.id])

    def test_post_with_delivery_address_id_stranger_group_stays_personal(self, db):
        """End-to-end pin of the delivery_address_id attack path (spec 5.1).

        delivery_address_id is the ONE route into PLACE scope that does not
        pass the pre-existing order-ownership check (there is no order_id at
        all here), so the resolver's intersection gate is its only defense.
        A customer who owns NO address in the group must not be able to grab
        PLACE scope over strangers' address group merely by naming their
        address id.
        """
        attacker = make_user(db)
        member_a, member_b = make_user(db), make_user(db)
        addr_a, addr_b = make_address(db, member_a), make_address(db, member_b)
        make_place_group(db, addr_a, addr_b)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=attacker.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            delivery_address_id=addr_a.id,
            recorded_by_user_id=admin.id,
            notes="attack via delivery_address_id, no order_id",
        )
        assert event.scope_type == "personal"
        assert event.scope_snapshot is None

    def test_post_with_delivery_address_id_group_member_grants_place_scope(self, db):
        """Legitimate use of delivery_address_id: poster owns a member address.

        No order_id is supplied either — delivery_address_id alone seeds the
        scope address for order-less standalone collections.
        """
        member, other_member = make_user(db), make_user(db)
        addr_member, addr_other = make_address(db, member), make_address(db, other_member)
        group = make_place_group(db, addr_member, addr_other)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=member.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            delivery_address_id=addr_member.id,
            recorded_by_user_id=admin.id,
            notes="legit cash via delivery_address_id, no order",
        )
        assert event.scope_type == "place"
        snap = event.scope_snapshot
        assert snap["group_id"] == group.id
        assert sorted(snap["address_ids"]) == sorted([addr_member.id, addr_other.id])
        assert sorted(snap["place_user_ids"]) == sorted([member.id, other_member.id])

    def test_post_stamps_cluster_scope_with_sorted_member_ids(self, db):
        """End-to-end pin of CLUSTER stamping via post_collection (spec 5.1)."""
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        order, _ = delivered_cod_order(db, u1)
        admin = make_user(db)
        event = CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("15000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="cluster cash collection",
        )
        assert event.scope_type == "cluster"
        assert event.scope_snapshot == {"user_ids": sorted([u1.id, u2.id])}
