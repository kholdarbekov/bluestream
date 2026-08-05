from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils.exceptions import ValidationError
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    link_users,
    make_address,
    make_place_group,
    make_user,
)


@pytest.mark.unit
class TestPlaceCodCap:
    def test_place_count_spans_owners(self, db):
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = make_place_group(db, a1, a2)
        delivered_cod_order(db, u1, address=a1)
        delivered_cod_order(db, u2, address=a2)
        svc = CashCollectionService()
        assert svc.get_place_active_cod_debt_count(a1.id) == 2
        assert svc.get_place_open_cod_debt_total(group.id) == Decimal("30000.00")

    def test_place_cap_blocks_clean_orderer(self, db):
        """UC3: two coworker debts at the office block a THIRD coworker with no
        debt of their own from ordering COD to the office."""
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u3)
        make_place_group(db, a1, a2, a3)
        delivered_cod_order(db, u1, address=a1)
        delivered_cod_order(db, u2, address=a2)
        svc = CashCollectionService()
        ctx = svc.get_cod_restriction_context(u3.id, delivery_address_id=a3.id)
        assert ctx["cod_restricted"] is True
        assert ctx["restriction_scope"] == "place"
        assert ctx["place_active_cod_debt_count"] == 2
        with pytest.raises(ValidationError) as exc:
            svc.validate_customer_can_use_cod(u3.id, delivery_address_id=a3.id)
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def test_home_debts_do_not_block_the_office(self, db):
        """UC4: non-grouped-address debts stay personal — they block the person,
        not the office (spec decision 1)."""
        u1, u2 = make_user(db), make_user(db)
        office1, office2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, office1, office2)
        home = make_address(db, u2)
        delivered_cod_order(db, u2, address=home)
        svc = CashCollectionService()
        ctx = svc.get_cod_restriction_context(u1.id, delivery_address_id=office1.id)
        assert ctx["place_active_cod_debt_count"] == 0
        assert ctx["cod_restricted"] is False

    def test_person_cap_reported_as_person_scope(self, db):
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        delivered_cod_order(db, u1)
        delivered_cod_order(db, u2)
        ctx = CashCollectionService().get_cod_restriction_context(u1.id)
        assert ctx["cod_restricted"] is True
        assert ctx["restriction_scope"] == "person"
        assert ctx["place_active_cod_debt_count"] is None

    def test_uc1_cluster_cap_blocks_sibling_account(self, db):
        """UC1 (spec §13 matrix): one person, N accounts, one address each.
        Debts booked on account A must block a COD order placed from account B
        to B's own (ungrouped) address — the cap is per PERSON, not per row."""
        svc = CashCollectionService()
        limit = svc.COD_ACTIVE_DEBT_LIMIT
        u1, u2 = make_user(db), make_user(db)
        link_users(db, [u1, u2])
        a1 = make_address(db, u1)
        a2 = make_address(db, u2)  # ungrouped — no place arm in play
        for _ in range(limit):
            delivered_cod_order(db, u1, address=a1)
        ctx = svc.get_cod_restriction_context(u2.id, delivery_address_id=a2.id)
        assert ctx["cod_restricted"] is True
        assert ctx["restriction_scope"] == "person"
        assert ctx["place_active_cod_debt_count"] is None
        with pytest.raises(ValidationError) as exc:
            svc.validate_customer_can_use_cod(u2.id, delivery_address_id=a2.id)
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def test_uc2_linked_cluster_with_merged_home_place_reports_place_scope(self, db):
        """UC2 (spec §13 matrix): N accounts of one person, some multi-address,
        with same-place addresses merged into a place group. Both cap arms can
        fire; this pins the PLACE arm in isolation — the cluster itself owes
        nothing, but a non-cluster member of the merged place is at the limit,
        so the clean cluster is still blocked FROM THAT PLACE.

        (Precedence note: when the cluster's OWN debts are the ones at the
        merged place, the person arm is evaluated first and
        `restriction_scope` reads 'person' — see
        `test_person_cap_reported_as_person_scope`.)"""
        svc = CashCollectionService()
        limit = svc.COD_ACTIVE_DEBT_LIMIT
        u1, u2, neighbour = make_user(db), make_user(db), make_user(db)
        link_users(db, [u1, u2])
        home1, home2 = make_address(db, u1), make_address(db, u2)
        neighbour_addr = make_address(db, neighbour)
        make_place_group(db, home1, home2, neighbour_addr)
        for _ in range(limit):
            delivered_cod_order(db, neighbour, address=neighbour_addr)
        ctx = svc.get_cod_restriction_context(u1.id, delivery_address_id=home1.id)
        assert ctx["active_cod_debt_count"] == 0
        assert ctx["cod_restricted"] is True
        assert ctx["restriction_scope"] == "place"
        assert ctx["place_active_cod_debt_count"] >= limit
        with pytest.raises(ValidationError) as exc:
            svc.validate_customer_can_use_cod(u1.id, delivery_address_id=home1.id)
        assert exc.value.error_code == "COD_DEBT_LIMIT_REACHED"

    def test_place_below_limit_does_not_block(self, db):
        """The place arm must not OVER-block: a grouped place one debt short of
        the limit still lets a clean coworker order COD."""
        svc = CashCollectionService()
        limit = svc.COD_ACTIVE_DEBT_LIMIT
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_place_group(db, a1, a2)
        for _ in range(limit - 1):
            delivered_cod_order(db, u1, address=a1)
        ctx = svc.get_cod_restriction_context(u2.id, delivery_address_id=a2.id)
        assert ctx["place_active_cod_debt_count"] == limit - 1
        assert ctx["cod_restricted"] is False
        assert ctx["restriction_scope"] is None
        assert svc.validate_customer_can_use_cod(u2.id, delivery_address_id=a2.id)["cod_restricted"] is False

    def test_ungrouped_address_reader_counts_only_that_address(self, db):
        """Pinned contract for Task 8 callers: the reader degrades to the single
        address for an ungrouped id (callers gate on groupedness), and the
        context's place arm stays OFF for it."""
        svc = CashCollectionService()
        limit = svc.COD_ACTIVE_DEBT_LIMIT
        owner, orderer = make_user(db), make_user(db)
        lone = make_address(db, owner)
        other = make_address(db, owner)  # ungrouped, same owner, different place
        orderer_addr = make_address(db, orderer)
        for _ in range(limit):
            delivered_cod_order(db, owner, address=lone)
        assert svc.get_place_active_cod_debt_count(lone.id) == limit
        assert svc.get_place_active_cod_debt_count(other.id) == 0
        # A different (ungrouped) address is never restricted by the place arm.
        ctx = svc.get_cod_restriction_context(orderer.id, delivery_address_id=orderer_addr.id)
        assert ctx["place_active_cod_debt_count"] is None
        assert ctx["cod_restricted"] is False

    def test_grocery_orderer_passes_capped_place(self, db):
        """The structural grocery exemption is the other cluster-OR arm: it, too,
        lets the ORDERER through a place that is at the limit."""
        u1, u2 = make_user(db), make_user(db)
        grocer = make_user(db, grocery=True)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, grocer)
        make_place_group(db, a1, a2, a3)
        delivered_cod_order(db, u1, address=a1)
        delivered_cod_order(db, u2, address=a2)
        svc = CashCollectionService()
        assert svc.get_place_active_cod_debt_count(a3.id) == 2
        ctx = svc.get_cod_restriction_context(grocer.id, delivery_address_id=a3.id)
        assert ctx["cod_restricted"] is False
        assert ctx["restriction_scope"] is None

    def test_place_count_ignores_owner_exemptions_but_orderer_exemption_wins(self, db):
        u1, u2, exempt_orderer = make_user(db), make_user(db), make_user(db, exempt=True)
        a1, a2, a3 = (
            make_address(db, u1),
            make_address(db, u2),
            make_address(db, exempt_orderer),
        )
        make_place_group(db, a1, a2, a3)
        delivered_cod_order(db, u1, address=a1)
        delivered_cod_order(db, u2, address=a2)
        svc = CashCollectionService()
        # place count includes exempt owners' debts…
        assert svc.get_place_active_cod_debt_count(a3.id) == 2
        # …but an exempt ORDERER is allowed, as today.
        ctx = svc.get_cod_restriction_context(exempt_orderer.id, delivery_address_id=a3.id)
        assert ctx["cod_restricted"] is False

    def test_no_address_arg_is_backward_compatible(self, db):
        u = make_user(db)
        svc = CashCollectionService()
        ctx = svc.get_cod_restriction_context(u.id)
        assert ctx["cod_restricted"] is False
        assert ctx["restriction_scope"] is None
        assert ctx["place_active_cod_debt_count"] is None
        assert svc.validate_customer_can_use_cod(u.id)["cod_restricted"] is False
