"""Integration/e2e coverage for the customer-link reconciliation invariants and
the unlink/place-group independence invariant.

Scope (see SHARED CONTRACT item 9 + the identity/geography orthogonality rule):

  * ``reconcile_customer_link_invariants()`` returns the report keys listed in
    ``_EMPTY_REPORT`` below — the balance-side checks
    (``negative_place_balances``, ``orphaned_place_balances``,
    ``stranded_address_balances``, ``invalid_scope_balances``), the identity
    checks (``orphaned_canonical_pointers``, ``grocery_or_entity_members``), the
    money checks (``events_missing_scope_snapshot``,
    ``allocation_stamp_mismatches``, ``event_conservation_violations``) and the
    two LEDGER-side oracles of the locking design (§6.2/§6.3:
    ``stamp_incoherent_ledger_entries``, ``duplicate_rescoped_ledger_entries``)
    — and on a clean state every list is empty. The key list is deliberately
    NOT enumerated a second time here in prose; ``_assert_completely_clean``
    asserts it, so a bucket added to the sweep later fails these tests instead
    of quietly going unchecked.
  * Cross-customer groups are a SANCTIONED state: a group whose member addresses
    span TWO canonical customers is legal (coworkers at one office), so the
    report carries no cross-customer key at all.
  * A PLACE whose single balance row is negative is flagged in
    ``negative_place_balances`` (by balance-row id). The old
    ``negative_group_unions`` / ``stranded_negative_pairs`` pair is gone: with
    one row per place, a negative sibling hiding inside a non-negative union is
    no longer representable, so that second key had nothing left to find.
  * A ``users.canonical_customer_id`` pointing at a non-existent canonical row is
    flagged in ``orphaned_canonical_pointers``.
  * After a normal ``CustomerLinkService.unlink_account`` the departing user's
    addresses KEEP their place-group membership (identity and geography are
    orthogonal in Phase 2 — bottles stay with the place, spec §8) and
    reconcile stays clean on the resulting cross-customer group.

Each test builds its own users/addresses/group via the function-scoped ``db``
fixture and asserts EXACT numeric balances with distinct values so a sign error
would be caught. All tests assert the CURRENT product contract; a failure here
would signal a product bug (left failing + reported, never patched away).
"""
from datetime import datetime, UTC, timedelta
from decimal import Decimal

import pytest

from business_app.models.user import User, UserAddress
from business_app.models.customer_link import CanonicalCustomer, AddressGroup
from business_app.models.bottle import BottleBalance
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from shared.enums import UserRole, UserStatus, UserType
from business_app.utils.password_security import hash_password


# --------------------------------------------------------------------------- #
# Builders — distinct phone per user; balances committed as exact Decimals.
# --------------------------------------------------------------------------- #

def _user(db, email, phone, *, created=None):
    u = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name="T",
        last_name="U",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=created or datetime.now(UTC),
    )
    db.session.add(u)
    db.session.commit()
    return u


def _addr(db, user_id, group_id=None):
    a = UserAddress(
        user_id=user_id,
        full_address="x, Tashkent",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
        address_group_id=group_id,
    )
    db.session.add(a)
    db.session.commit()
    return a


def _bal(db, address, amount):
    """Seed the PLACE row for `address` (group-keyed when grouped, else address-keyed)."""
    b = BottleBalance(
        address_group_id=address.address_group_id,
        address_id=None if address.address_group_id is not None else address.id,
        balance=Decimal(str(amount)),
    )
    db.session.add(b)
    db.session.commit()
    return b


def _canonical(db, primary_user_id):
    c = CanonicalCustomer(primary_user_id=primary_user_id)
    db.session.add(c)
    db.session.commit()
    return c


def _group(db, canonical_id=None, label="home"):
    g = AddressGroup(canonical_customer_id=canonical_id, label=label)
    db.session.add(g)
    db.session.commit()
    return g


# The sweep's ENTIRE surface. Written as a literal, and compared with `==`
# rather than key-by-key, so that a bucket added later CANNOT escape:
#
#   * a NEW key that this literal does not carry makes every `report ==
#     _EMPTY_REPORT` below fail on the shape, before it ever gets to the values
#     — the new check is then either declared clean here or shown to be dirty,
#     but it cannot be silently unexamined;
#   * a key that is REMOVED from the sweep fails the same way, so a check that
#     is quietly deleted cannot leave a test asserting a promise nobody keeps.
#
# `_assert_completely_clean` below states that intent as an assertion instead of
# a comment, because "the `==` also checks the keys" is exactly the kind of
# incidental property that gets refactored away.
_EMPTY_REPORT = {
    "negative_place_balances": [],
    "orphaned_canonical_pointers": [],
    "grocery_or_entity_members": [],
    "events_missing_scope_snapshot": [],
    "allocation_stamp_mismatches": [],
    "event_conservation_violations": [],
    "orphaned_place_balances": [],
    "stranded_address_balances": [],
    # A PENDING fine frozen to a place with no members left — the state that
    # becomes an `orphaned_place_balances` row the moment somebody settles it.
    "stranded_fine_scopes": [],
    "invalid_scope_balances": [],
    # ORACLE 2 / ORACLE 3 of the locking design (§6.2/§6.3) — the two
    # LEDGER-side checks. Both must be empty on every clean world below; the
    # states that make them fire live in
    # `tests/unit/test_customer_link_reconciliation.py` and
    # `tests/integration/test_place_conservation_invariants_e2e.py`.
    "stamp_incoherent_ledger_entries": [],
    "duplicate_rescoped_ledger_entries": [],
    "group_check_errors": [],
}


def _assert_completely_clean(report):
    """Assert the sweep found NOTHING — over every bucket it has, not a subset.

    The key-set assertion comes first so a sixth check arriving in the sweep
    fails as "the sweep grew a bucket this test has never looked at" rather than
    as an opaque dict diff. The value assertion then covers all of them at once,
    including any bucket added to `_EMPTY_REPORT` afterwards, so nothing has to
    be remembered at each call site.
    """
    assert set(report) == set(_EMPTY_REPORT), (
        "the sweep's key set changed: "
        f"{sorted(set(report) ^ set(_EMPTY_REPORT))}. A new violation class is "
        "not covered by these clean-world tests until it is declared in "
        "_EMPTY_REPORT — decide whether this world is clean under it."
    )
    assert report == _EMPTY_REPORT


@pytest.mark.integration
class TestReconciliationInvariants:
    def test_clean_state_every_list_empty(self, db):
        """A well-formed cluster (one canonical, one group, non-negative union,
        valid pointers) yields every invariant list empty."""
        u1 = _user(db, "clean-a@example.com", "+998900000101")
        u2 = _user(db, "clean-b@example.com", "+998900000102")
        canonical = _canonical(db, u1.id)
        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()
        group = _group(db, canonical.id)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)  # same physical place, phone-2
        _bal(db, a1, "5.00")  # ONE pooled row for the place (was 3 + 2 per pair)
        assert a2.address_group_id == group.id

        report = reconcile_customer_link_invariants()

        _assert_completely_clean(report)

    def test_cross_customer_group_is_sanctioned(self, db):
        """Phase 2: one group spanning TWO canonical customers is legal — the
        report contains no cross-customer key and stays clean."""
        u1 = _user(db, "xc-a@example.com", "+998900000111")
        u2 = _user(db, "xc-b@example.com", "+998900000112")
        c1 = _canonical(db, u1.id)
        c2 = _canonical(db, u2.id)
        u1.canonical_customer_id = c1.id
        u2.canonical_customer_id = c2.id
        db.session.commit()
        group = _group(db, None, label="shared-office")
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "2.00")  # ONE pooled row for the shared office (was 1 + 1)
        assert a2.address_group_id == group.id

        report = reconcile_customer_link_invariants()

        assert "address_group_cross_customer" not in report
        _assert_completely_clean(report)

    def test_negative_place_balance_is_flagged(self, db):
        """A PLACE whose balance row is < 0 is flagged in negative_place_balances.

        Was `test_negative_group_union_is_flagged`, which seeded -5 on u1's pair
        and +2 on u2's so the SUM came to -3. One row per place means the -3 is
        now the row itself; the flagged value is the balance-row id rather than
        the group id, because the row is what a remediator has to fix.
        """
        u1 = _user(db, "neg-a@example.com", "+998900000121")
        u2 = _user(db, "neg-b@example.com", "+998900000122")
        canonical = _canonical(db, u1.id)
        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()
        group = _group(db, canonical.id)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        # Over-collected at this shared place: the pooled row is -3.
        row = _bal(db, a1, "-3.00")
        assert a2.address_group_id == group.id

        # Sanity: the place really is negative, read from EITHER member address.
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("-3.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("-3.00")

        report = reconcile_customer_link_invariants()

        assert row.id in report["negative_place_balances"]
        assert report["orphaned_canonical_pointers"] == []
        # The group is live (both addresses still point at it), so it is not an
        # orphan — the negative key owns this finding on its own.
        assert report["orphaned_place_balances"] == []

    def test_zero_place_balance_is_not_flagged(self, db):
        """Boundary: a place balance of exactly 0 (net-neutral, e.g. +4 delivered
        then -4 collected) is NOT negative and must not be flagged."""
        u1 = _user(db, "zero-a@example.com", "+998900000151")
        u2 = _user(db, "zero-b@example.com", "+998900000152")
        canonical = _canonical(db, u1.id)
        u1.canonical_customer_id = canonical.id
        u2.canonical_customer_id = canonical.id
        db.session.commit()
        group = _group(db, canonical.id)
        a1 = _addr(db, u1.id, group_id=group.id)
        a2 = _addr(db, u2.id, group_id=group.id)
        _bal(db, a1, "0.00")
        assert a2.address_group_id == group.id

        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")

        report = reconcile_customer_link_invariants()

        _assert_completely_clean(report)

    def test_orphaned_canonical_pointer_is_flagged(self, db):
        """A user.canonical_customer_id pointing at a non-existent canonical row
        is flagged in orphaned_canonical_pointers."""
        u = _user(db, "orphan@example.com", "+998900000131")
        # Dangling pointer: no canonical_customers row with this id exists.
        # (SQLite test DB runs with FK enforcement off — see project memory.)
        u.canonical_customer_id = 999999
        db.session.commit()

        report = reconcile_customer_link_invariants()

        assert u.id in report["orphaned_canonical_pointers"]
        assert report["negative_place_balances"] == []

    def test_valid_canonical_pointer_is_not_orphaned(self, db):
        """Control for the orphan check: a pointer at an existing canonical row
        must NOT be reported."""
        u = _user(db, "valid-ptr@example.com", "+998900000161")
        canonical = _canonical(db, u.id)
        u.canonical_customer_id = canonical.id
        db.session.commit()

        report = reconcile_customer_link_invariants()

        assert report["orphaned_canonical_pointers"] == []


@pytest.mark.integration
class TestUnlinkGroupIndependenceInvariant:
    def test_unlink_leaves_group_membership_intact_and_reconcile_stays_clean(self, db):
        """Phase 2: unlink detaches identity only — the place group keeps BOTH
        addresses (now a sanctioned cross-customer group) and reconcile stays
        clean. Driven through the real service write-path."""
        u1 = _user(db, "elink-a@example.com", "+998900000141",
                   created=datetime.now(UTC) - timedelta(days=5))
        u2 = _user(db, "elink-b@example.com", "+998900000142")
        admin = _user(db, "elink-admin@example.com", "+998900000149")
        svc = CustomerLinkService()

        link = svc.link_accounts(u1.id, u2.id, actor_admin_id=admin.id, reason="link")
        canonical_id = link["canonical_customer_id"]

        a1 = _addr(db, u1.id)
        a2 = _addr(db, u2.id)
        group = svc.create_place_group([a1.id, a2.id], acting_admin_id=admin.id, reason="same home")
        db.session.refresh(a1)
        _bal(db, a1, "3.00")  # ONE pooled row for the place (was 2 + 1 per pair)

        _assert_completely_clean(reconcile_customer_link_invariants())

        svc.unlink_account(u2.id, actor_admin_id=admin.id, reason="mislink")

        db.session.refresh(a2); db.session.refresh(u2)
        db.session.refresh(a1); db.session.refresh(u1)
        assert u2.canonical_customer_id is None
        assert a2.address_group_id == group.id        # NOT ejected — geography survives
        assert u1.canonical_customer_id == canonical_id
        assert a1.address_group_id == group.id

        # Cross-customer membership after unlink is sanctioned; reconcile clean.
        _assert_completely_clean(reconcile_customer_link_invariants())
