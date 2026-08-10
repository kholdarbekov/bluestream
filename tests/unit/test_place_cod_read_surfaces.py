"""Plan 2c / Task 2 — PLACE-scoped and CLUSTER-scoped COD *read* surfaces.

Nothing here moves money. These are the staff/admin decision-support reads that
Phase 2 needs once a debt can belong to a *place* (an ownerless address group
spanning different customers) rather than only to a person:

  * ``get_place_cod_statement`` — the unified open COD debt at one place,
    whoever ordered it,
  * ``get_place_cod_debtor_rows`` / the mixed ``paginate_users_with_open_cod_debts``
    list — place rows first, then cluster-collapsed person rows,
  * ``get_customer_cod_statement`` — cluster totals and place context alongside
    the (still per-account) items.

The regression baseline is the unlinked + ungrouped customer: every one of these
surfaces must degrade to exactly today's answer for them.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.customer_link import CanonicalCustomer
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import NotFoundError
from business_app.utils.password_security import hash_password
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus, UserRole, UserType

LAT, LNG = 41.3111, 69.2797


def _user(db, email, phone, *, exempt=False):
    u = User(email=email, phone=phone, password_hash=hash_password("TestPassword123!"),
             first_name="T", last_name=email.split("@")[0], user_type=UserType.INDIVIDUAL,
             role=UserRole.CUSTOMER, is_verified=True, cod_debt_check_exempt=exempt,
             created_at=datetime.now(UTC))
    db.session.add(u)
    db.session.commit()
    return u


def _address(db, user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office",
                    latitude=LAT, longitude=LNG)
    db.session.add(a)
    db.session.commit()
    return a


def _delivered_cod_debt(db, user, order_number, *, address=None, outstanding=Decimal("15000.00")):
    order = Order(user_id=user.id, order_number=order_number, status=OrderStatus.DELIVERED,
                  subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
                  discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                  total_amount=Decimal("15000.00"), payment_method=PaymentMethod.CASH,
                  delivery_address_id=address.id if address else None,
                  created_at=datetime.now(UTC))
    db.session.add(order)
    db.session.flush()
    payment = Payment(order_id=order.id, user_id=user.id, payment_method=PaymentMethod.CASH,
                      amount=Decimal("15000.00"), currency="UZS", status=PaymentStatus.PENDING,
                      payment_id=f"pay_{order_number}", outstanding_amount=outstanding,
                      created_at=datetime.now(UTC))
    db.session.add(payment)
    db.session.commit()
    return order, payment


def _link(db, users):
    canonical = CanonicalCustomer(primary_user_id=users[0].id)
    db.session.add(canonical)
    db.session.commit()
    for u in users:
        u.canonical_customer_id = canonical.id
    db.session.commit()
    return canonical


def _place(db, addresses, admin, label=None):
    return CustomerLinkService().create_place_group(
        [a.id for a in addresses], acting_admin_id=admin.id, reason="coworkers", label=label)


@pytest.mark.unit
class TestPlaceCodStatement:
    def test_place_statement_unifies_members_debts(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = _place(db, [a1, a2], admin, label="Office 7")
        _delivered_cod_debt(db, u1, "ORD-1", address=a1, outstanding=Decimal("15000.00"))
        _delivered_cod_debt(db, u2, "ORD-2", address=a2, outstanding=Decimal("20000.00"))
        # A debt at u1's OTHER (ungrouped) address must not count.
        home = _address(db, u1)
        _delivered_cod_debt(db, u1, "ORD-3", address=home)

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert stmt["place_group_id"] == group.id
        assert stmt["label"] == "Office 7"
        assert stmt["member_count"] == 2
        assert stmt["active_cod_debt_count"] == 2
        assert stmt["total_outstanding_amount"] == 35000.0
        owners = {i["owner_user_id"] for i in stmt["items"]}
        assert owners == {u1.id, u2.id}
        assert all(i["owner_name"] for i in stmt["items"])
        # The ungrouped-address debt is excluded item-by-item, not just in the sum.
        assert {i["order_number"] for i in stmt["items"]} == {"ORD-1", "ORD-2"}
        item = next(i for i in stmt["items"] if i["order_number"] == "ORD-2")
        assert item["outstanding_amount"] == 20000.0
        assert item["owner_user_id"] == u2.id
        assert item["owner_name"] == u2.full_name
        assert item["created_at"] is not None

    def test_place_statement_with_no_open_debt_is_empty_but_counts_members(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        group = _place(db, [_address(db, u1), _address(db, u2)], admin)

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert stmt["items"] == []
        assert stmt["total_outstanding_amount"] == 0.0
        assert stmt["active_cod_debt_count"] == 0
        assert stmt["member_count"] == 2

    def test_place_statement_counts_owners_not_addresses(self, db):
        """One person with two grouped addresses is ONE member, not two."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        group = _place(db, [_address(db, u1), _address(db, u1), _address(db, u2)], admin)

        assert CashCollectionService().get_place_cod_statement(group.id)["member_count"] == 2

    def test_place_statement_excludes_undelivered(self, db):
        """Cash offered against a PENDING order settles nothing, so it is not debt."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = _place(db, [a1, a2], admin)
        order, payment = _delivered_cod_debt(db, u1, "ORD-PENDING", address=a1)
        order.status = OrderStatus.PENDING
        db.session.commit()

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert stmt["items"] == []
        assert stmt["total_outstanding_amount"] == 0.0

    def test_place_statement_includes_a_repriced_electronic_receivable(self, db):
        """A card order edited upward after settlement IS collectible debt.

        Inverted on 2026-08-08 from the old
        `test_place_statement_excludes_undelivered_and_non_cash`, which asserted
        that a CLICK payment with a real outstanding on a delivered order was
        excluded from the workplace statement — the driver standing at that
        office was shown nothing owed and collected nothing (prod order 961).

        PARTIALLY_PAID is the *only* electronic state that qualifies: the card
        already settled and the order then grew. An unpaid Click order is
        deliberately NOT here — see the sibling test below and
        `open_receivable_clause`'s docstring.
        """
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = _place(db, [a1, a2], admin)
        card_order, card_payment = _delivered_cod_debt(db, u2, "ORD-CARD", address=a2)
        card_payment.payment_method = PaymentMethod.CLICK
        card_payment.status = PaymentStatus.PARTIALLY_PAID
        card_payment.amount_collected = Decimal("5000.00")
        card_payment.amount = Decimal("20000.00")
        db.session.commit()

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert {i["order_number"] for i in stmt["items"]} == {"ORD-CARD"}
        assert stmt["total_outstanding_amount"] == 15000.0

    def test_place_statement_excludes_an_unpaid_electronic_order(self, db):
        """🔴 Money-safety guard: a live gateway payment is not ledger debt.

        Every unpaid Click row carries a positive `outstanding_amount` (seeded by
        `Payment.__init__`). If such a row entered the place statement it would
        also enter the allocation rings, and a COWORKER's cash at this very
        office could be absorbed by it — then destroyed when the customer paid
        the Click link. Those orders are settled through an explicit target
        (conversion), never a ring walk.
        """
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = _place(db, [a1, a2], admin)
        card_order, card_payment = _delivered_cod_debt(db, u2, "ORD-UNPAID", address=a2)
        card_payment.payment_method = PaymentMethod.CLICK
        card_payment.status = PaymentStatus.PENDING
        db.session.commit()

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert stmt["items"] == []
        assert stmt["total_outstanding_amount"] == 0.0

    def test_place_statement_excludes_a_settled_electronic_payment(self, db):
        """Guard rail: a COMPLETED prepaid payment is never debt, stale column or not."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        group = _place(db, [a1, a2], admin)
        card_order, card_payment = _delivered_cod_debt(db, u2, "ORD-SETTLED", address=a2)
        card_payment.payment_method = PaymentMethod.CLICK
        card_payment.status = PaymentStatus.COMPLETED
        db.session.commit()

        stmt = CashCollectionService().get_place_cod_statement(group.id)
        assert stmt["items"] == []
        assert stmt["total_outstanding_amount"] == 0.0

    def test_missing_group_raises_not_found(self, db):
        with pytest.raises(NotFoundError):
            CashCollectionService().get_place_cod_statement(999999)


@pytest.mark.unit
class TestDebtorRows:
    def test_place_rows_and_cluster_collapse(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        _place(db, [a1, a2], admin)
        _delivered_cod_debt(db, u1, "ORD-1", address=a1)
        _delivered_cod_debt(db, u2, "ORD-2", address=a2)
        # A separate linked person with two accounts, one debt each, no place.
        u3 = _user(db, "c@example.com", "+998900000003")
        u4 = _user(db, "d@example.com", "+998900000004")
        _link(db, [u3, u4])
        _delivered_cod_debt(db, u3, "ORD-4")
        _delivered_cod_debt(db, u4, "ORD-5")

        result = CashCollectionService().paginate_users_with_open_cod_debts(page=1, per_page=10)
        items = result["items"]
        place_rows = [r for r in items if r.get("row_type") == "place"]
        person_rows = [r for r in items if r.get("row_type") == "person"]
        assert len(place_rows) == 1
        assert place_rows[0]["member_count"] == 2
        assert place_rows[0]["total_outstanding_amount"] == 30000.0
        # u3+u4 collapse into ONE cluster row with the combined figures.
        cluster_rows = [r for r in person_rows if set(r["member_user_ids"]) == {u3.id, u4.id}]
        assert len(cluster_rows) == 1
        assert cluster_rows[0]["cluster_member_count"] == 2
        assert cluster_rows[0]["active_cod_debt_count"] == 2
        assert cluster_rows[0]["total_outstanding_amount"] == 30000.0
        # Place rows always sort ahead of person rows.
        assert items[0]["row_type"] == "place"

    def test_place_row_membership_and_totals(self, db):
        """A 3-person office with a single debtor still reports 3 members."""
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        u3 = _user(db, "c@example.com", "+998900000003")
        a1, a2, a3 = _address(db, u1), _address(db, u2), _address(db, u3)
        group = _place(db, [a1, a2, a3], admin, label="Floor 3")
        _delivered_cod_debt(db, u1, "ORD-1", address=a1, outstanding=Decimal("12000.00"))
        _delivered_cod_debt(db, u1, "ORD-2", address=a1, outstanding=Decimal("8000.00"))

        [row] = CashCollectionService().get_place_cod_debtor_rows()
        assert row == {
            "row_type": "place",
            "place_group_id": group.id,
            "label": "Floor 3",
            "member_count": 3,
            "debtor_member_count": 1,
            "active_cod_debt_count": 2,
            "total_outstanding_amount": 20000.0,
        }

    def test_place_rows_sorted_by_outstanding_desc(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        small = [_user(db, "s1@example.com", "+998900000001"), _user(db, "s2@example.com", "+998900000002")]
        big = [_user(db, "b1@example.com", "+998900000003"), _user(db, "b2@example.com", "+998900000004")]
        sa = [_address(db, u) for u in small]
        ba = [_address(db, u) for u in big]
        small_group = _place(db, sa, admin, label="small")
        big_group = _place(db, ba, admin, label="big")
        _delivered_cod_debt(db, small[0], "ORD-S", address=sa[0], outstanding=Decimal("5000.00"))
        _delivered_cod_debt(db, big[0], "ORD-B", address=ba[0], outstanding=Decimal("90000.00"))

        rows = CashCollectionService().get_place_cod_debtor_rows()
        assert [r["place_group_id"] for r in rows] == [big_group.id, small_group.id]

    def test_place_row_absent_when_place_has_no_open_debt(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        _place(db, [_address(db, u1), _address(db, u2)], admin)
        assert CashCollectionService().get_place_cod_debtor_rows() == []

    def test_unlinked_ungrouped_rows_unchanged(self, db):
        u = _user(db, "solo@example.com", "+998900000007")
        _delivered_cod_debt(db, u, "ORD-9")
        result = CashCollectionService().paginate_users_with_open_cod_debts(page=1, per_page=10)
        [row] = result["items"]
        assert row["row_type"] == "person"
        assert row["id"] == u.id
        assert row["cluster_member_count"] == 1
        assert row["member_user_ids"] == [u.id]
        assert row["active_cod_debt_count"] == 1
        assert row["total_outstanding_amount"] == 15000.0
        assert row["cod_restricted"] is False
        assert result["pagination"] == {"page": 1, "per_page": 10, "total": 1, "pages": 1}

    def test_collapsed_row_keeps_cluster_aware_restriction_flag(self, db):
        """Regression (2b Task 9): the flag is exemption-aware, not a raw count."""
        exempt = _user(db, "exempt@example.com", "+998900000006", exempt=True)
        _delivered_cod_debt(db, exempt, "ORD-E1")
        _delivered_cod_debt(db, exempt, "ORD-E2")
        rows = CashCollectionService().list_users_with_open_cod_debts()
        row = next(r for r in rows if r["id"] == exempt.id)
        assert row["active_cod_debt_count"] == 2
        assert row["cod_restricted"] is False

    def test_collapsed_row_identity_is_the_largest_debtor(self, db):
        small = _user(db, "small@example.com", "+998900000001")
        large = _user(db, "large@example.com", "+998900000002")
        _link(db, [small, large])
        _delivered_cod_debt(db, small, "ORD-S", outstanding=Decimal("1000.00"))
        _delivered_cod_debt(db, large, "ORD-L", outstanding=Decimal("90000.00"))

        rows = CashCollectionService().list_users_with_open_cod_debts()
        assert len(rows) == 1
        assert rows[0]["id"] == large.id
        assert rows[0]["phone"] == large.phone
        assert rows[0]["member_user_ids"] == sorted([small.id, large.id])
        assert rows[0]["total_outstanding_amount"] == 91000.0
        assert rows[0]["active_cod_debt_count"] == 2

    def test_list_rows_gain_cluster_keys_and_no_place_rows(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        a1, a2 = _address(db, u1), _address(db, u2)
        _place(db, [a1, a2], admin)
        _delivered_cod_debt(db, u1, "ORD-1", address=a1)

        rows = CashCollectionService().list_users_with_open_cod_debts()
        assert all("row_type" not in r or r["row_type"] == "person" for r in rows)
        assert [r["id"] for r in rows] == [u1.id]
        assert rows[0]["cluster_member_count"] == 1


@pytest.mark.unit
class TestStatementClusterAndPlaceContext:
    def test_statement_gains_cluster_totals_and_places(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        _link(db, [u1, u2])
        a1 = _address(db, u1)
        other = _user(db, "x@example.com", "+998900000005")
        ax = _address(db, other)
        _place(db, [a1, ax], admin, label="Office 7")
        _delivered_cod_debt(db, u1, "ORD-1", address=a1)
        _delivered_cod_debt(db, u2, "ORD-2")
        stmt = CashCollectionService().get_customer_cod_statement(u1.id)
        assert stmt["cluster_member_count"] == 2
        assert stmt["cluster_delivered_outstanding_amount"] == 30000.0
        assert len(stmt["places"]) == 1
        place = stmt["places"][0]
        assert place["address_id"] == a1.id
        assert place["label"] == "Office 7"
        assert place["place_open_cod_debt_total"] == 15000.0
        assert place["place_active_cod_debt_count"] == 1

    def test_statement_place_total_includes_other_members_debt(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        other = _user(db, "x@example.com", "+998900000005")
        a1, ax = _address(db, u1), _address(db, other)
        group = _place(db, [a1, ax], admin)
        _delivered_cod_debt(db, u1, "ORD-1", address=a1, outstanding=Decimal("15000.00"))
        _delivered_cod_debt(db, other, "ORD-2", address=ax, outstanding=Decimal("25000.00"))

        stmt = CashCollectionService().get_customer_cod_statement(u1.id)
        [place] = stmt["places"]
        assert place["place_group_id"] == group.id
        assert place["place_open_cod_debt_total"] == 40000.0
        assert place["place_active_cod_debt_count"] == 2
        # ...while the person's own ledger stays per-account.
        assert stmt["total_outstanding_amount"] == 15000.0

    def test_statement_lists_one_row_per_group_across_the_cluster(self, db):
        admin = _user(db, "adm@example.com", "+998900000009")
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        _link(db, [u1, u2])
        other = _user(db, "x@example.com", "+998900000005")
        # u2 (the sibling) owns the grouped address; u1's statement must see it.
        a2, ax = _address(db, u2), _address(db, other)
        group = _place(db, [a2, ax], admin)
        _delivered_cod_debt(db, u2, "ORD-2", address=a2)

        stmt = CashCollectionService().get_customer_cod_statement(u1.id)
        assert [p["place_group_id"] for p in stmt["places"]] == [group.id]
        assert stmt["places"][0]["address_id"] == a2.id

    def test_statement_asymmetry_is_explainable_from_the_payload(self, db):
        """A linked sibling with no debts of their own reports a CLUSTER count
        over an EMPTY per-account item list. The payload must let a surface say
        so instead of guessing."""
        u1 = _user(db, "a@example.com", "+998900000001")
        u2 = _user(db, "b@example.com", "+998900000002")
        _link(db, [u1, u2])
        _delivered_cod_debt(db, u1, "ORD-1")

        stmt = CashCollectionService().get_customer_cod_statement(u2.id)
        assert stmt["active_cod_debt_count"] == 1          # cluster-wide
        assert stmt["account_active_cod_debt_count"] == 0   # this account
        assert stmt["items"] == []
        assert stmt["total_outstanding_amount"] == 0.0      # this account
        assert stmt["cluster_delivered_outstanding_amount"] == 15000.0
        assert stmt["cluster_member_count"] == 2

    def test_unlinked_statement_places_empty_and_cluster_equals_own(self, db):
        u = _user(db, "solo@example.com", "+998900000007")
        _delivered_cod_debt(db, u, "ORD-9")
        stmt = CashCollectionService().get_customer_cod_statement(u.id)
        assert stmt["cluster_member_count"] == 1
        assert stmt["cluster_delivered_outstanding_amount"] == 15000.0
        assert stmt["account_active_cod_debt_count"] == stmt["active_cod_debt_count"] == 1
        assert stmt["total_outstanding_amount"] == 15000.0
        assert stmt["places"] == []

    def test_ungrouped_addresses_produce_no_place_rows(self, db):
        u = _user(db, "solo@example.com", "+998900000007")
        _address(db, u)
        _address(db, u)
        _delivered_cod_debt(db, u, "ORD-9")
        assert CashCollectionService().get_customer_cod_statement(u.id)["places"] == []
