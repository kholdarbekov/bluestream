"""Preview/apply parity for corrections under a frozen scope (Plan 2b, spec §5.6).

Task 4's fix wave already made ``adjust_event_amount`` **apply** under the
original event's frozen scope (pinned by
``tests/unit/test_correction_frozen_scope_replay.py``). What this module pins is
the other half: every read-only PROJECTION an admin approves before confirming
must walk the SAME candidate universe the live allocator walks.

Three surfaces, one rule:

* ``CashCollectionService.simulate_event_amount_change`` — the projection behind
  the collected-cash edit modal. It must resolve the event's FROZEN scope, not
  the poster's single account.
* ``OrderCashEditService.preview`` — its spill warning must see the whole scope,
  and it must flag a correction that pushes the cluster or the place back over
  the COD active-debt cap.
* ``CashCollectionService.preview_personal_card_transfer`` — its own docstring
  promises "the admin modal cannot drift from what actually happens when they
  confirm", so it must resolve the scope through the same entry point the post
  does (grocery backstop included).

The parity tests below do not assert a hand-computed projection and hope it
matches: they run the projection, then run the real correction, and compare.
They fail if the two ever diverge, whichever side moves.

Spec: docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md
"""
from decimal import Decimal

import pytest

from business_app.models.payment import CashCollectionAllocation, Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_cash_edit_service import OrderCashEditService
from shared.enums import CashCollectionSource, EntitySubtype, OrderStatus, UserType
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


def _outstanding(db, payment):
    db.session.refresh(payment)
    return Decimal(str(payment.outstanding_amount))


def _live_debt_allocations(event):
    """Live allocations that actually moved a payment's projection (ring 1/2).

    Reservations are excluded deliberately: they are ring-3, releasable,
    forward-looking state and are NOT what the modal's debt figures describe.
    """
    return [
        a
        for a in event.allocations
        if a.reversed_at is None and CashCollectionService._allocation_affects_payment_projection(a)
    ]


def _apply_and_measure(db, service, event_id, *, new_amount, admin, order_id):
    """Run the REAL correction and report it in ``simulate_...`` vocabulary."""
    replacement = service.adjust_event_amount(
        event_id,
        new_amount=new_amount,
        adjusted_by_user_id=admin.id,
        reason="parity measurement",
    )
    db.session.refresh(replacement)
    live = _live_debt_allocations(replacement)
    order_payment = Payment.query.filter_by(order_id=order_id).first() if order_id else None
    return {
        "applied_to_order": sum(
            (Decimal(str(a.allocated_amount)) for a in live if a.order_id == order_id),
            Decimal("0.00"),
        ),
        "applied_total": sum((Decimal(str(a.allocated_amount)) for a in live), Decimal("0.00")),
        "credit_after": Decimal(str(replacement.unapplied_amount)),
        "order_outstanding_after": (
            Decimal(str(order_payment.outstanding_amount)) if order_payment else Decimal("0.00")
        ),
    }


def _projection_slice(projection):
    return {
        "applied_to_order": projection["applied_to_order"],
        "applied_total": projection["applied_total"],
        "credit_after": projection["credit_after"],
        "order_outstanding_after": projection["order_outstanding_after"],
    }


@pytest.mark.unit
class TestCorrectionsFrozenScope:
    def test_adjust_after_ungroup_replays_place_scope(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("10000.00")
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("10000.00")
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("20000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert event.scope_type == "place"
        assert _outstanding(db, coworker_payment) == Decimal("0.00")
        # Topology change between post and correction: dissolve the group.
        a1.address_group_id = None
        a2.address_group_id = None
        db.session.commit()
        replacement = svc.adjust_event_amount(
            event.id,
            new_amount=Decimal("20000.00"),
            adjusted_by_user_id=admin.id,
            reason="re-book same amount",
        )
        # Frozen PLACE scope: the coworker's debt is STILL settled by the repost.
        db.session.refresh(coworker_payment)
        db.session.refresh(own_payment)
        assert Decimal(str(coworker_payment.outstanding_amount)) == Decimal("0.00")
        assert Decimal(str(own_payment.outstanding_amount)) == Decimal("0.00")
        assert replacement.scope_type == "place"
        assert sorted(replacement.scope_snapshot["address_ids"]) == sorted([a1.id, a2.id])
        assert coworker_order.id != own_order.id

    def test_simulate_matches_frozen_scope(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        delivered_cod_order(db, u2, address=a2, total=Decimal("10000.00"))
        own_order, _ = delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("20000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        a1.address_group_id = None
        a2.address_group_id = None
        db.session.commit()
        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("20000.00"), order_id=own_order.id
        )
        # The whole 20k still applies within the frozen place scope: nothing
        # becomes credit even though the group no longer exists.
        assert projection["applied_total"] == Decimal("20000.00")
        assert projection["credit_after"] == Decimal("0.00")

    def test_ring3_carveout_no_reservation_resurrection_after_unlink(self, db):
        """Spec 5.6: post -> unlink -> adjust must not resurrect out-of-scope
        reservations — the sweep resolves against the CURRENT cluster."""
        u1, sibling, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, sibling])
        own_order, _ = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, pending_sibling_payment = delivered_cod_order(
            db, sibling, total=Decimal("4000.00"), status=OrderStatus.CONFIRMED
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("9000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="overpaid",
        )
        # Surplus swept onto the sibling's pending order while linked.
        assert (
            CashCollectionAllocation.query.filter_by(
                payment_id=pending_sibling_payment.id,
                allocation_mode="prepaid_reservation",
            ).filter(CashCollectionAllocation.reversed_at.is_(None)).count()
            == 1
        )
        # Unlink the sibling (bare pointer detach is enough for scope math).
        sibling.canonical_customer_id = None
        db.session.commit()
        svc.adjust_event_amount(
            event.id,
            new_amount=Decimal("9000.00"),
            adjusted_by_user_id=admin.id,
            reason="re-book same amount",
        )
        live = (
            CashCollectionAllocation.query.filter_by(
                payment_id=pending_sibling_payment.id,
                allocation_mode="prepaid_reservation",
            )
            .filter(CashCollectionAllocation.reversed_at.is_(None))
            .count()
        )
        assert live == 0, "correction must not recreate reservations on a departed sibling"


@pytest.mark.unit
class TestSimulatePreviewEqualsApply:
    """The whole reason ``simulate_event_amount_change`` exists.

    Each test projects, then performs the real correction, then compares. A
    per-account projection over a cluster/place event fails on
    ``applied_to_order`` — the admin approves "10 000 lands on this order" and
    causes "6 000 lands on this order, 6 000 on someone else's".
    """

    def test_preview_equals_apply_for_a_linked_customer(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _sibling_order, sibling_payment = delivered_cod_order(db, u2, total=Decimal("6000.00"))
        own_order, own_payment = delivered_cod_order(db, u1, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("4000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="part payment",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, sibling_payment) == Decimal("2000.00")

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("12000.00"), order_id=own_order.id
        )
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("12000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        # …and the projection is not vacuously equal: money really crossed accounts.
        assert actual["applied_to_order"] == Decimal("6000.00")
        assert _outstanding(db, sibling_payment) == Decimal("0.00")
        assert _outstanding(db, own_payment) == Decimal("4000.00")

    def test_preview_matches_apply_when_customer_becomes_grocery_after_the_post(self, db):
        """``post_collection`` force-overrides the replay to PERSONAL scope when
        the event's customer is CURRENTLY a grocery store (spec 5.8 layer 3),
        even though the event was stamped CLUSTER at post time. The projection
        must mirror that same backstop, or the preview promises a sibling
        spill the apply can never perform.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        _sibling_order, sibling_payment = delivered_cod_order(db, u2, total=Decimal("6000.00"))
        own_order, own_payment = delivered_cod_order(db, u1, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("4000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="part payment",
        )
        assert event.scope_type == "cluster"
        assert _outstanding(db, sibling_payment) == Decimal("2000.00")

        # Convert the account to a grocery store AFTER the event was stamped.
        u1.user_type = UserType.ENTITY
        u1.entity_subtype = EntitySubtype.GROCERY_STORE
        db.session.commit()
        assert u1.is_grocery_store is True

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("12000.00"), order_id=own_order.id
        )
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("12000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        # Personal scope only: the correction settles the customer's own
        # order and nothing more spills onto the sibling's debt. The void
        # step of the correction also unwinds the ORIGINAL cluster-scoped
        # 4000 that had been applied to the sibling, and the personal-scope
        # repost never touches it again, so the sibling's debt is back to
        # its full, untouched total.
        assert actual["applied_to_order"] == Decimal("10000.00")
        assert _outstanding(db, sibling_payment) == Decimal("6000.00")
        assert _outstanding(db, own_payment) == Decimal("0.00")

    def test_preview_equals_apply_for_a_place_grouped_customer(self, db):
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        _coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("6000.00")
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("10000.00")
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("4000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash, part payment",
        )
        assert event.scope_type == "place"
        assert _outstanding(db, coworker_payment) == Decimal("2000.00")

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("12000.00"), order_id=own_order.id
        )
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("12000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        assert actual["applied_to_order"] == Decimal("6000.00")
        assert _outstanding(db, coworker_payment) == Decimal("0.00")
        assert _outstanding(db, own_payment) == Decimal("4000.00")

    def test_preview_equals_apply_when_a_place_debt_outranks_an_older_cluster_debt(self, db):
        """Rings are a priority list, not one flat oldest-first list.

        The projection must reproduce ring 1 BEFORE ring 2, not merely widen the
        set of accounts it looks at: the orderer's own older debt at an ungrouped
        address is ring 2 and is paid AFTER the coworker's newer debt at the
        place. Flattening the two rings changes what lands on the order.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        # Oldest of all three, but ring 2: delivered to an address outside the place.
        _elsewhere_order, elsewhere_payment = delivered_cod_order(db, u1, total=Decimal("6000.00"))
        _coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("5000.00")
        )
        own_order, own_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("10000.00")
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("2000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert event.scope_type == "place"
        # Ring 1 first even though the ungrouped debt is older.
        assert _outstanding(db, coworker_payment) == Decimal("3000.00")
        assert _outstanding(db, elsewhere_payment) == Decimal("6000.00")

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("20000.00"), order_id=own_order.id
        )
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("20000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        # 5000 coworker + 10000 own (both ring 1) then 5000 of the ring-2 debt.
        assert actual["applied_to_order"] == Decimal("10000.00")
        assert _outstanding(db, coworker_payment) == Decimal("0.00")
        assert _outstanding(db, own_payment) == Decimal("0.00")
        assert _outstanding(db, elsewhere_payment) == Decimal("1000.00")

    def test_preview_equals_apply_after_the_place_group_is_dissolved(self, db):
        """Parity must survive a topology change: both sides read the SNAPSHOT."""
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        _coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("6000.00")
        )
        own_order, _own_payment = delivered_cod_order(
            db, u1, address=a1, total=Decimal("10000.00")
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("4000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        a1.address_group_id = None
        a2.address_group_id = None
        db.session.commit()

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("12000.00"), order_id=own_order.id
        )
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("12000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        assert _outstanding(db, coworker_payment) == Decimal("0.00")

    def test_personal_scope_projection_is_unchanged(self, db):
        """Unlinked + ungrouped stays byte-identical: surplus becomes credit."""
        u1, admin = make_user(db), make_user(db)
        _stranger_order, stranger_payment = delivered_cod_order(
            db, make_user(db), total=Decimal("9000.00")
        )
        own_order, _own_payment = delivered_cod_order(db, u1, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=own_order.id,
            recorded_by_user_id=admin.id,
            notes="paid in full",
        )
        assert event.scope_type == "personal"

        projection = svc.simulate_event_amount_change(
            event=event, new_amount=Decimal("13000.00"), order_id=own_order.id
        )
        assert projection["applied_to_order"] == Decimal("10000.00")
        assert projection["credit_after"] == Decimal("3000.00")
        actual = _apply_and_measure(
            db, svc, event.id, new_amount=Decimal("13000.00"), admin=admin, order_id=own_order.id
        )
        assert _projection_slice(projection) == actual
        # The stranger is never in anybody's universe.
        assert _outstanding(db, stranger_payment) == Decimal("9000.00")


@pytest.mark.unit
class TestOrderCashEditScopeParity:
    def test_preview_flags_cap_breach_on_correction_down(self, db, app):
        u = make_user(db)
        admin = make_user(db)
        # Two other open debts -> cluster already at the limit of 2.
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order, payment = delivered_cod_order(db, u, total=Decimal("10000.00"))
        svc = CashCollectionService()
        # Use the real DELIVERY_COMPLETION event shape via direct post (no
        # delivery row needed for the service-level preview path). Post EXACTLY
        # ONE cash event for this order: a second one makes
        # OrderCashEditService._resolve_event append `multiple_cash_events` and
        # return None, silently killing the assertion under test.
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        # Retarget the event source so OrderCashEditService resolves it.
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)
        assert payment is not None

    def test_no_cap_warning_for_a_cod_exempt_cluster_at_the_limit(self, db, app):
        """A COD-exempt customer can never actually be capped (spec 5.5's

        admin-exemption arm), so a correction that leaves their cluster's raw
        debt count at/over the limit must not warn that they will be locked
        out of COD — they never were.
        """
        u = make_user(db, exempt=True)
        admin = make_user(db)
        # Two other open debts -> raw cluster count already at the limit of 2,
        # but the exemption means the cap never actually applies to them.
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order, payment = delivered_cod_order(db, u, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert not any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)
        assert payment is not None

    def test_cap_warning_counts_a_reopened_debt_that_is_settled_today(self, db, app):
        """`becomes_open_debt` arm: the order is settled NOW, so the cluster
        count does not include it — the correction re-opens it and tips the
        cluster to the limit."""
        u, admin = make_user(db), make_user(db)
        # Target is the OLDEST, so the collection settles it rather than the other debt.
        order, order_payment = delivered_cod_order(db, u, total=Decimal("10000.00"))
        delivered_cod_order(db, u, total=Decimal("15000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()
        assert _outstanding(db, order_payment) == Decimal("0.00")
        assert svc.get_cluster_active_cod_debt_count(u.id) == 1

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)

    def test_cap_warning_fires_on_the_PLACE_arm_with_the_cluster_under_the_limit(self, db, app):
        """The `or projected_place >= limit` arm, isolated.

        The orderer's own cluster ends the edit with ONE open debt — under the
        limit — so only the place arm can fire. Without it the admin re-opens a
        debt that silently locks the whole workplace out of COD while the modal
        says nothing: a place is capped by spec 5.5 even when no single person
        there is.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        # Own order is the OLDEST, so the place-scoped collection settles it and
        # leaves the coworker's debt open.
        order, order_payment = delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
        _coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("6000.00")
        )
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert event.scope_type == "place"
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()
        assert _outstanding(db, order_payment) == Decimal("0.00")
        assert _outstanding(db, coworker_payment) == Decimal("6000.00")
        # Person arm cannot fire: u1's cluster has 0 open debts now, 1 after the edit.
        assert svc.get_cluster_active_cod_debt_count(u1.id) == 0
        # Place arm will: 1 coworker debt open + this order re-opening == the limit.
        assert svc.get_place_active_cod_debt_count(a1.id) == 1
        assert svc.COD_ACTIVE_DEBT_LIMIT == 2

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)

    def test_no_cap_warning_when_the_cluster_stays_under_the_limit(self, db, app):
        u, admin = make_user(db), make_user(db)
        order, order_payment = delivered_cod_order(db, u, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()
        assert _outstanding(db, order_payment) == Decimal("0.00")

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert not any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)

    def test_no_cap_warning_when_the_correction_settles_the_order(self, db, app):
        """Correcting UP (order ends settled) can never breach the cap."""
        u, admin = make_user(db), make_user(db)
        delivered_cod_order(db, u)
        delivered_cod_order(db, u)
        order, _ = delivered_cod_order(db, u, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("1000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("40000.00"))
        assert not any(w.startswith("correction_pushes_cod_over_cap") for w in plan.warnings)

    def test_spill_warning_sees_a_place_coworkers_debt(self, db, app):
        """The spill warning must be resolved from the event's SCOPE.

        The coworker's open debt belongs to a different account, so a
        per-account lookup reports "no other unpaid orders" while the correction
        would in fact settle the coworker's order first.
        """
        u1, u2, admin = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        delivered_cod_order(db, u2, address=a2, total=Decimal("6000.00"))
        order, _ = delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u1.id,
            amount=Decimal("2000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="office cash",
        )
        assert event.scope_type == "place"
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("3000.00"))
        assert any(w.startswith("customer_has_other_unpaid_cod_orders") for w in plan.warnings)

    def test_no_spill_warning_for_a_lone_unlinked_customer(self, db, app):
        u, admin = make_user(db), make_user(db)
        # A stranger's open debt must never trip the warning.
        delivered_cod_order(db, make_user(db), total=Decimal("6000.00"))
        order, _ = delivered_cod_order(db, u, total=Decimal("10000.00"))
        svc = CashCollectionService()
        event = svc.post_collection(
            customer_id=u.id,
            amount=Decimal("10000.00"),
            source="standalone_meeting",
            order_id=order.id,
            recorded_by_user_id=admin.id,
            notes="collected",
        )
        event.source = CashCollectionSource.DELIVERY_COMPLETION
        db.session.commit()

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("9000.00"))
        assert not any(w.startswith("customer_has_other_unpaid_cod_orders") for w in plan.warnings)


@pytest.mark.unit
class TestPersonalCardTransferPreviewScope:
    def test_grocery_preview_never_spills_onto_a_linked_account(self, db):
        """The apply path FORCES personal scope for grocery money (spec §5.8
        layer 3, pinned by
        ``test_ring_allocation.py::test_grocery_customer_never_spills_onto_a_linked_account``).
        A preview resolving the cluster from current topology promises the
        admin a spill that can never happen."""
        grocery = make_user(db, grocery=True)
        sibling = make_user(db)
        link_users(db, [grocery, sibling])
        _sibling_order, sibling_payment = delivered_cod_order(db, sibling, total=Decimal("6000.00"))
        target_order, _ = delivered_cod_order(db, grocery, total=Decimal("5000.00"))

        plan = CashCollectionService().preview_personal_card_transfer(
            order_id=target_order.id, amount=Decimal("9000.00")
        )
        assert plan.applied_to_order == Decimal("5000.00")
        assert plan.spill_allocations == []
        assert plan.applied_to_other_debts == Decimal("0.00")
        assert plan.remaining_as_credit == Decimal("4000.00")
        assert _outstanding(db, sibling_payment) == Decimal("6000.00")

    def test_place_grouped_preview_never_spills_onto_a_coworker(self, db):
        """Coworkers are not a wallet — and this preview now DEPENDS on that.

        Before Task 7 the spill universe was a bare ``get_cluster_user_ids``
        call, which could not reach a coworker no matter what. It now goes
        through ``resolve_allocation_scope`` with the order's delivery address,
        so the only thing keeping a card transfer off a colleague's debt is
        ``PERSONAL_CARD_TRANSFER`` being absent from ``_PLACE_SCOPE_SOURCES``
        (spec 5.1: identifiably the payer's OWN money). That is a new coupling;
        pin it here, or a one-line edit to that frozenset silently starts paying
        strangers' debts with a named person's card money.
        """
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        _coworker_order, coworker_payment = delivered_cod_order(
            db, u2, address=a2, total=Decimal("6000.00")
        )
        target_order, _ = delivered_cod_order(db, u1, address=a1, total=Decimal("5000.00"))

        plan = CashCollectionService().preview_personal_card_transfer(
            order_id=target_order.id, amount=Decimal("9000.00")
        )
        assert plan.applied_to_order == Decimal("5000.00")
        assert plan.spill_allocations == []
        assert plan.applied_to_other_debts == Decimal("0.00")
        assert plan.remaining_as_credit == Decimal("4000.00")
        assert _outstanding(db, coworker_payment) == Decimal("6000.00")

    def test_linked_preview_still_covers_the_cluster(self, db):
        """The grocery backstop must narrow ONLY grocery — a normal linked payer
        still sees the sibling spill."""
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        target_order, _ = delivered_cod_order(db, u1, total=Decimal("5000.00"))
        _, sibling_payment = delivered_cod_order(db, u2, total=Decimal("3000.00"))
        plan = CashCollectionService().preview_personal_card_transfer(
            order_id=target_order.id, amount=Decimal("8000.00")
        )
        assert plan.applied_to_order == Decimal("5000.00")
        assert plan.applied_to_other_debts == Decimal("3000.00")
        assert plan.remaining_as_credit == Decimal("0.00")
        assert plan.spill_allocations[0]["order_id"] == sibling_payment.order_id
