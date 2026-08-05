"""E2E: the PLACE-GROUP LIFECYCLE — create, join, leave, re-add, dissolve, delete fence.

One axis, exhaustively: every membership transition a place can go through, and
what each one is allowed to do to the bottles, to the ledger, to the audit trail
and to the three address-delete entry points.

Four rules this file applies everywhere:

* **The pair, never one side.** A membership edit MOVES bottles; it never mints
  or destroys them. Every transition asserts `Σ bottle_balances` before and
  after (`_stored_total`), not just the number the place ends up showing. Assert
  only "the place holds 6 now" and a bug that also stranded 4 somewhere else
  sails through.
* **Real write paths only.** Balances are built with `admin_adjust_balance`,
  `record_bottles_delivered` and `record_bottles_returned` against real `Order`
  rows; groups are built with `create_place_group` / `add_addresses_to_group` /
  `remove_address_from_group`; HTTP tests use the real routes with real JWTs.
  No `BottleBalance` row is ever hand-built. Two fixtures delete LEDGER rows to
  reproduce the dev-DB drift shape (address 24: a stored figure with no entries
  explaining it) — each says so at the point of use, and the balance row itself
  is always the one the service really wrote.
* **A rejection writes NOTHING** — no ledger row, no balance movement, no
  `CustomerLinkEvent`, no `AddressGroup`, and the membership pointer is
  unchanged. Asserted WITHOUT an intervening `db.session.rollback()`, because
  the rollback is exactly what would hide a flushed-but-uncommitted phantom.
* **Drift is preserved, never repaired, by a membership edit.** A place's STORED
  balance (`bottle_balances.balance`, what `get_place_balance` returns) and its
  LEDGER SUM legitimately disagree on production data. The join CARRIES the
  stored figure; the dissolve MOVES the stored figure. Only the merge review
  (a different axis) converges them, and only `reconcile_balance` destroys the
  difference.

Deliberately NOT re-tested here (already covered by a sibling file, verified by
reading it):
  * the `bottles_leaving` input domain, its cap and its rounding — that is
    `test_place_split_full_e2e.py`'s whole axis, including the two-concurrent-
    removals-from-a-THREE-member-group bug and the split-vs-join lock ordering;
  * the dissolve's paired `place_dissolve` entries, attribution and the
    survivor's snapshots — `test_place_dissolve_and_delete_fence.py`;
  * per-write conservation of deliveries/returns/fines/adjustments and the join
    fences' "writes nothing" property — `test_place_conservation_invariants_e2e.py`;
  * the merge review, its preview and its backfill — `test_place_merge_review_full_e2e.py`;
  * COD/money state across a dissolve — `test_place_money_boundary_e2e.py`;
  * customer/driver-facing rendering of these transitions — the two bot files.

Bugs pinned here rather than fixed (see the `xfail` markers and the run notes):
`set_initial_balance` silently no-ops after a join-then-leave cycle, and again
at GROUP scope on a dissolved-then-repopulated place, where the 200 response
hands the admin a DIFFERENT customer's ledger row; a repopulated dissolved group
inherits the departed strangers' ledger (and arms the Reconcile button against
the new occupant); `OrderEditService._cascade_bottle` re-resolves the bottle
scope LIVE, so correcting an already-delivered order books to wherever its
address is TODAY rather than to the scope the `delivery:{order}` row it corrects
is stamped to; join and dissolve silently DESTROY a place's `last_delivery_at` /
`last_return_at` and nothing rebuilds them; a customer's own two-tap "I moved"
re-points a place member to another building, unfenced and unaudited; a place
with exactly ONE owner is constructible three ways and §7.3 structurally cannot
collapse it; the account-merge path is a FOURTH address-delete entry point that
no CALL-SITE fence can reach, because it deletes through the `User.addresses`
relationship cascade rather than through a `db.session.delete(address)`
statement; and two concurrent removals from a two-member place collide on row
locks on real Postgres.

Three classes here fail production code MID-WRITE rather than at a fence
(`TestAJoinThatFailsMidLoop`, and the two `..._that_CRASHES_...` tests), because
every other "a rejection writes NOTHING" test in this file rejects BEFORE the
first write. They were each verified to go red by deleting the route's
`_rollback_db_session()` — without it the audit event survives a rolled-back
removal, a dissolve destroys an unexplained drift permanently, and a join loses
the balance row it already deleted.

The merge bug is pinned on BOTH databases on purpose, because the two disagree
and only one of them is production. On SQLite the member vanishes and its ledger
rows dangle; on Postgres the same cascade is refused outright when the member
has orders (`ck_orders_address_required_after_pending`, and the failure escapes
`auto_link_accounts` as a 500 instead of `{'success': False}`) and succeeds —
member silently gone, no audit event — only when it has none. The dangling
`bottle_ledger.address_id` in the SQLite pin is an FK-off artifact and is
labelled as one; the Postgres pair in section N is the production statement.
"""

import itertools
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, inspect, text

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product, ProductCategory
from business_app.models.subscription import Subscription
from business_app.models.translation import Translation
from business_app.models.user import User, UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import (
    BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    BottleTrackingService,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
    SubscriptionFrequency,
    SubscriptionStatus,
    UserRole,
    UserStatus,
    UserType,
)

pytestmark = [pytest.mark.integration, pytest.mark.e2e]


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

_SEQ = itertools.count(1)


def _user(db, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL, **extra):
    """A real user row. `is_grocery_store` is a DERIVED read-only property
    (`user_type=ENTITY` + `entity_subtype=GROCERY_STORE`), so a grocery account is
    built through those two real columns, never by setting the property."""
    n = next(_SEQ)
    user = User(
        email=f"place-life-{n}@example.com",
        phone=f"+99871{n:07d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=f"F{n}",
        last_name=f"L{n}",
        user_type=user_type,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
        **extra,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _addr(db, user_id, title="Office"):
    address = UserAddress(
        user_id=user_id,
        title=title,
        full_address=f"desk {next(_SEQ)}, Tashkent",
        city="Tashkent",
        latitude=41.31,
        longitude=69.28,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _admin(db):
    return _user(db, role=UserRole.ADMIN, user_type=UserType.STAFF)


def _order(db, user, address):
    order = Order(
        user_id=user.id,
        order_number=f"ORD-LIFE-{next(_SEQ)}",
        status=OrderStatus.DELIVERED,
        subtotal=Decimal("0.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("0.00"),
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _seed(db, address, user, qty, notes="seed"):
    """Put `qty` bottles at this address's PLACE through the real write path."""
    entry = BottleTrackingService().admin_adjust_balance(
        user_id=user.id,
        address_id=address.id,
        adjustment=Decimal(str(qty)),
        actor_user_id=user.id,
        notes=notes,
    )
    db.session.commit()
    return entry


def _deliver(db, user, address, qty):
    order = _order(db, user, address)
    entry = BottleTrackingService().record_bottles_delivered(
        order.id, user.id, address.id, Decimal(str(qty))
    )
    db.session.commit()
    return entry


def _give_back(db, user, address, qty):
    order = _order(db, user, address)
    entry = BottleTrackingService().record_bottles_returned(
        user.id, address.id, Decimal(str(qty)), order_id=order.id
    )
    db.session.commit()
    return entry


def _place(address_id):
    """The operational read: what the PLACE this address belongs to holds."""
    return BottleTrackingService.get_place_balance(address_id)


def _group_row(group_id):
    return BottleBalance.query.filter_by(address_group_id=group_id).one_or_none()


def _own_row(address_id):
    return BottleBalance.query.filter(
        BottleBalance.address_id == address_id, BottleBalance.address_group_id.is_(None)
    ).one_or_none()


def _stored_total():
    """Σ of every `bottle_balances` row in the database — the conservation figure."""
    total = _db.session.query(
        func.coalesce(func.sum(BottleBalance.balance), Decimal("0.00"))
    ).scalar()
    return Decimal(str(total or 0))


def _coupled_total():
    """Σ of every BALANCE-COUPLED ledger quantity (the decoupled backfill excluded)."""
    key = func.coalesce(BottleLedger.idempotency_key, "")
    query = _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
    for prefix in BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES:
        query = query.filter(key.notlike(f"{prefix}%"))
    return Decimal(str(query.scalar() or 0))


def _ledger_sum(scope):
    return Decimal(
        str(
            _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
            .filter(*scope.ledger_filter())
            .scalar()
            or 0
        )
    )


def _group_ledger_sum(group_id):
    return _ledger_sum(BottleScope.for_group(group_id))


def _own_ledger_sum(address_id):
    return _ledger_sum(BottleScope.for_address(address_id))


def _snapshot():
    """Everything a REJECTED lifecycle call must leave untouched, comparable in one value."""
    return {
        "ledger_rows": BottleLedger.query.count(),
        "balance_rows": BottleBalance.query.count(),
        "stored_total": _stored_total(),
        "coupled_total": _coupled_total(),
        "link_events": CustomerLinkEvent.query.count(),
        "groups": AddressGroup.query.count(),
        "memberships": sorted(
            (a.id, a.address_group_id) for a in UserAddress.query.all()
        ),
    }


def _sweep():
    """The nightly invariant sweep, reduced to the keys a lifecycle edit can break."""
    report = reconcile_customer_link_invariants()
    return {
        key: report[key]
        for key in (
            "orphaned_place_balances",
            "stranded_address_balances",
            "invalid_scope_balances",
            "grocery_or_entity_members",
            "negative_place_balances",
        )
    }


_CLEAN_SWEEP = {
    "orphaned_place_balances": [],
    "stranded_address_balances": [],
    "invalid_scope_balances": [],
    "grocery_or_entity_members": [],
    "negative_place_balances": [],
}


def _events(group_id):
    return CustomerLinkService().get_place_group_events(group_id)


def _entry_facts(entry):
    """Every column of a ledger entry EXCEPT the two the re-scoping may rewrite."""
    columns = {c.key for c in inspect(BottleLedger).mapper.column_attrs}
    return {
        name: getattr(entry, name)
        for name in sorted(columns - {"address_group_id", "balance_after", "updated_at"})
    }


def _leave_rows(address_id):
    return (
        BottleLedger.query.filter(
            BottleLedger.address_id == address_id,
            BottleLedger.idempotency_key.like("place_leave:%"),
        )
        .order_by(BottleLedger.id.asc())
        .all()
    )


def _token(app, user, *, role=None):
    from flask_jwt_extended import create_access_token

    claims = {"role": role} if role else None
    with app.app_context():
        return create_access_token(identity=str(user.id), additional_claims=claims)


def _headers(app, user, *, role=None):
    return {
        "Authorization": f"Bearer {_token(app, user, role=role)}",
        "Content-Type": "application/json",
    }


class _Place:
    """A place group over N distinct customers, one address each."""

    def __init__(self, db, member_count=3, label="office"):
        self.db = db
        self.svc = CustomerLinkService()
        self.admin = _admin(db)
        self.users = [_user(db) for _ in range(member_count)]
        self.addrs = [_addr(db, u.id) for u in self.users]
        group = self.svc.create_place_group(
            [a.id for a in self.addrs],
            acting_admin_id=self.admin.id,
            reason="same office",
            label=label,
        )
        self.group_id = group.id

    @property
    def a(self):
        return self.addrs[0]

    @property
    def b(self):
        return self.addrs[1]

    @property
    def c(self):
        return self.addrs[2]

    @property
    def ua(self):
        return self.users[0]

    @property
    def ub(self):
        return self.users[1]

    def remove(self, address, **kwargs):
        kwargs.setdefault("reason", "left the office")
        return self.svc.remove_address_from_group(
            address.id, acting_admin_id=self.admin.id, **kwargs
        )

    def add(self, addresses, **kwargs):
        kwargs.setdefault("reason", "moved in")
        return self.svc.add_addresses_to_group(
            self.group_id, [a.id for a in addresses], acting_admin_id=self.admin.id, **kwargs
        )


def _funded_place(db, qty, member_count=3):
    place = _Place(db, member_count=member_count)
    if Decimal(str(qty)) != 0:
        _seed(db, place.a, place.ua, qty)
    assert _place(place.a.id) == Decimal(str(qty)).quantize(Decimal("0.01"))
    return place


def _assert_refused(action, error_code):
    """Run `action`, require a NAMED ValidationError, and leave the session clean.

    Used where a transition the suite used to exercise is now a refusal: the
    refusal IS the transition, and the invariants asserted around it must hold
    after it just as they did after the write it replaced.
    """
    with pytest.raises(ValidationError) as exc:
        action()
    assert exc.value.error_code == error_code, exc.value.error_code
    _db.session.rollback()


def _force_membership(db, address_id, group_id):
    """Point an address at a group WITHOUT going through the service.

    NO SERVICE PATH BUILDS A ONE-MEMBER PLACE ANY MORE. `create_place_group`
    requires >= 2; a removal that would leave one member DISSOLVES in the same
    transaction; and a dissolved (memberless) group is now refused as a join
    target (`PLACE_GROUP_DISSOLVED`), which was the last door — a dissolved group
    could be re-populated to exactly one member and then emptied.

    The shape is still REACHABLE IN PRODUCTION DATA written before that refusal
    landed, and `_dissolve_if_last_member`'s ZERO-REMAINING arm exists precisely
    for it (it is also why `release_group_history_to_address` must pass
    `allow_memberless=True`). So the tests that cover that arm build the state by
    hand rather than being retired — a code path with no test is how the arm
    would quietly rot.
    """
    db.session.query(UserAddress).filter(UserAddress.id == address_id).update(
        {UserAddress.address_group_id: group_id}, synchronize_session=False
    )
    db.session.commit()
    db.session.expire_all()


def _drop_ledger_for_group(db, group_id):
    """Reproduce the dev-DB drift shape (address 24): a STORED figure whose ledger
    no longer explains it. Only LEDGER rows are removed — the `bottle_balances`
    row is the one the real service wrote."""
    BottleLedger.query.filter_by(address_group_id=group_id).delete(synchronize_session=False)
    db.session.commit()


def _drop_ledger_for_member(db, group_id, address_id):
    """The same dev-DB drift shape, but leaving ONE member's entries in place, so
    a dissolve has both an explained part (`own_sum`) and an unexplained
    remainder that is NOT re-derivable from any ledger. Only LEDGER rows are
    removed; the `bottle_balances` row stays the one the real service wrote."""
    BottleLedger.query.filter(
        BottleLedger.address_group_id == group_id, BottleLedger.address_id == address_id
    ).delete(synchronize_session=False)
    db.session.commit()


def _bottle_product(db, per_unit="1"):
    """A real returnable-bottle product, so `OrderEditService` has something to
    cascade from. `returnable_bottles_per_unit` is what `_cascade_bottle`
    multiplies the item delta by."""
    n = next(_SEQ)
    category = ProductCategory(name=f"Water-{n}", description="w", is_active=True)
    db.session.add(category)
    db.session.commit()
    product = Product(
        name=f"Pure Water {n}",
        description="d",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=1000,
        min_stock_level=1,
        max_stock_level=5000,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal(str(per_unit)),
        created_at=datetime.now(UTC),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _order_with_item(db, user, product, address, *, quantity, status):
    order = _order(db, user, address)
    order.status = status
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * Decimal(str(quantity)),
        )
    )
    db.session.commit()
    return order


def _delivery_rows(order_id):
    return BottleLedger.query.filter_by(idempotency_key=f"delivery:{order_id}").all()


def _keys_like(pattern):
    return sorted(
        e.idempotency_key
        for e in BottleLedger.query.filter(BottleLedger.idempotency_key.like(pattern)).all()
    )


def _read_every_surface(client, app, *, admin, driver, address, owner, order):
    """ONE place, read through SIX independent surfaces, in the units a human sees.

    Each of the ten files in this effort builds only its own surface's fixtures,
    so an inconsistency BETWEEN surfaces — the admin panel reading 12 while the
    customer bot reads 7 and the driver's card reads 12 — is unreachable by every
    one of them. The long-arc tests that exist assert a GLOBAL SUM, which is
    exactly the oracle a misattribution satisfies. This is the oracle that does
    not: not "did the total change" but "does every screen agree about which
    place holds what".

    The driver card is read through `business_app.api.staff`'s own two helpers
    rather than through `GET /staff/delivery/active`, because that route
    additionally needs a `Delivery` row, a driver assignment and the route
    optimiser — none of which touch the bottle figure. Those two functions ARE
    the card's bottle computation, verbatim.
    """
    from business_app.api.staff import _customer_bottle_balance, _place_bottle_balance_signed

    scope = BottleTrackingService.resolve_scope(address.id)

    listing = client.get("/api/v1/admin/bottles/balances", headers=_headers(app, admin))
    assert listing.status_code == 200, listing.get_json()
    items = listing.get_json()["data"]["items"]
    if scope.is_grouped:
        admin_rows = [i for i in items if i["address_group_id"] == scope.group_id]
    else:
        admin_rows = [
            i for i in items
            if i["address_id"] == scope.address_id and i["address_group_id"] is None
        ]

    customer = client.get("/api/v1/orders/bottles/my-balances", headers=_headers(app, owner))
    assert customer.status_code == 200, customer.get_json()
    customer_rows = [
        r for r in customer.get_json()["data"]["balances"] if r["address_id"] == address.id
    ]

    picker = client.get(
        f"/api/v1/staff/bottles/customer/{owner.id}/addresses", headers=_headers(app, driver)
    )
    assert picker.status_code == 200, picker.get_json()
    picker_rows = [r for r in picker.get_json()["data"] if r["address_id"] == address.id]

    statement = client.get(
        f"/api/v1/staff/bottles/customer/{owner.id}/summary", headers=_headers(app, driver)
    )
    assert statement.status_code == 200, statement.get_json()
    statement_rows = [
        r for r in statement.get_json()["data"]["addresses"] if r["address_id"] == address.id
    ]

    def _one(rows, key):
        # A place that has never moved a bottle has no row anywhere; every
        # surface renders that as zero, so the absent row must read as zero here
        # too or the oracle would be satisfied by a MISSING screen.
        assert len(rows) <= 1, rows
        return Decimal(str(rows[0][key])) if rows else Decimal("0.00")

    return {
        "service.get_place_balance": BottleTrackingService.get_place_balance(address.id),
        "admin.balances_row": _one(admin_rows, "balance"),
        "customer.my_balances": _one(customer_rows, "place_balance"),
        "staff.picker_row": _one(picker_rows, "place_balance"),
        "staff.statement": _one(statement_rows, "place_balance"),
        "driver.card_signed": Decimal(str(_place_bottle_balance_signed(order))),
        "driver.card_anchor": Decimal(str(_customer_bottle_balance(order))),
    }


def _live_scope_rows_are_well_formed():
    """One `bottle_balances` row per live scope, every one of them addressable."""
    rows = BottleBalance.query.all()
    for row in rows:
        BottleTrackingService.assert_scope_row_valid(row)
    keys = [(r.address_group_id, r.address_id) for r in rows]
    assert len(keys) == len(set(keys)), keys
    return keys


# =========================================================================== #
# A. Creating a place
# =========================================================================== #


class TestCreateAPlace:
    def test_two_funded_addresses_collapse_into_one_place_with_a_restamped_ledger(self, db):
        """The whole join in one assertion set: ONE row for the place, the joiners'
        own rows gone, every one of their ledger entries carrying the group, and
        both members reading the same single pool."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        e1 = _deliver(db, u1, a1, "6")
        e2 = _deliver(db, u2, a2, "5")
        total_before = _stored_total()
        assert total_before == Decimal("11.00")

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="same office"
        )

        assert _stored_total() == total_before                      # the pair
        assert BottleBalance.query.count() == 1
        row = _group_row(group.id)
        assert row is not None and row.address_id is None
        assert row.balance == Decimal("11.00")
        assert _own_row(a1.id) is None and _own_row(a2.id) is None
        assert _place(a1.id) == _place(a2.id) == Decimal("11.00")
        # Both entries now live in the place's scope, and the place's ledger
        # explains the place's figure.
        db.session.refresh(e1)
        db.session.refresh(e2)
        assert e1.address_group_id == e2.address_group_id == group.id
        assert _group_ledger_sum(group.id) == Decimal("11.00")
        assert _own_ledger_sum(a1.id) == Decimal("0.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_the_join_is_order_independent_and_absorbs_all_three_in_one_call(self, db):
        """`_absorb_joiners_into_group` iterates `sorted(addresses)` but reads its
        anchor from the UNSORTED list. Passing the ids out of order must produce
        the identical place: same balance, same sorted `rescoped_ledger_entry_ids`."""
        outcomes = []
        for order in ("shuffled", "sorted"):
            admin = _admin(db)
            users = [_user(db) for _ in range(3)]
            addrs = [_addr(db, u.id) for u in users]
            entries = [
                _deliver(db, users[0], addrs[0], "2"),
                _deliver(db, users[1], addrs[1], "3"),
                _deliver(db, users[2], addrs[2], "4"),
            ]
            ids = [a.id for a in addrs]
            passed = [ids[2], ids[0], ids[1]] if order == "shuffled" else sorted(ids)

            group = CustomerLinkService().create_place_group(
                passed, acting_admin_id=admin.id, reason="three desks"
            )

            event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").order_by(
                CustomerLinkEvent.id.desc()
            ).first()
            outcomes.append(
                {
                    "balance": _place(ids[0]),
                    "rescoped_count": len(event.event_metadata["rescoped_ledger_entry_ids"]),
                    "rescoped_is_sorted": event.event_metadata["rescoped_ledger_entry_ids"]
                    == sorted(event.event_metadata["rescoped_ledger_entry_ids"]),
                    "rescoped_matches_entries": set(
                        event.event_metadata["rescoped_ledger_entry_ids"]
                    )
                    == {e.id for e in entries},
                    "own_rows": [_own_row(i) for i in ids],
                    "member_count": UserAddress.query.filter_by(address_group_id=group.id).count(),
                }
            )

        shuffled, in_order = outcomes
        # Order independence stated as ONE equality, so a future key added to the
        # outcome dict is compared too rather than silently going unasserted.
        assert shuffled == in_order
        assert shuffled["balance"] == Decimal("9.00")
        assert shuffled["rescoped_count"] == 3
        assert shuffled["rescoped_is_sorted"] is True
        assert shuffled["rescoped_matches_entries"] is True
        assert shuffled["own_rows"] == in_order["own_rows"] == [None, None, None]
        assert shuffled["member_count"] == 3

    def test_an_own_row_reading_exactly_zero_is_DELETED_and_no_group_row_is_created(self, db):
        """`absorbed == 0` short-circuits creating the place's row — a place that
        never moved a bottle must not gain a 0.00 row just for being grouped.
        The joiner's own row must still be deleted: leaving it behind strands a
        row every place-scoped read resolves past, which only the nightly
        `stranded_address_balances` sweep can see."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _deliver(db, u1, a1, "4")
        _give_back(db, u1, a1, "4")
        assert _own_row(a1.id) is not None and _own_row(a1.id).balance == Decimal("0.00")
        assert BottleLedger.query.filter_by(address_id=a1.id).count() == 2

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="empty office"
        )

        assert _own_row(a1.id) is None
        assert _group_row(group.id) is None
        assert BottleBalance.query.count() == 0
        assert _place(a1.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance_row(a1.id) is None
        # ...and both entries followed the address into the place.
        assert BottleLedger.query.filter_by(address_group_id=group.id).count() == 2
        assert _group_ledger_sum(group.id) == Decimal("0.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_an_over_returned_address_joins_with_its_NEGATIVE_figure(self, db):
        """A place legitimately going negative is the state `max(0, place)` exists
        for. A join that clamped the absorbed figure at 0 would MINT bottles."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _deliver(db, u1, a1, "2")
        _give_back(db, u1, a1, "5")                 # -3 at a1
        _deliver(db, u2, a2, "5")
        assert _place(a1.id) == Decimal("-3.00")
        total_before = _stored_total()

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )

        assert _place(a1.id) == Decimal("2.00")
        assert _stored_total() == total_before == Decimal("2.00")
        assert _group_ledger_sum(group.id) == Decimal("2.00")
        assert _sweep()["negative_place_balances"] == []

    def test_a_place_that_ends_up_negative_is_reported_by_the_nightly_sweep(self, db):
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _deliver(db, u1, a1, "2")
        _give_back(db, u1, a1, "10")                # -8 at a1
        _deliver(db, u2, a2, "5")

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )

        assert _place(a1.id) == Decimal("-3.00")
        assert _sweep()["negative_place_balances"] == [_group_row(group.id).id]

    def test_fractional_balances_survive_the_join_exactly(self, db):
        """`Decimal(str(...))` all the way through. A float anywhere in the carry
        or in the running-snapshot accumulator shows up first on fractions."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _seed(db, a1, u1, "1.50")
        _seed(db, a2, u2, "2.25")

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )

        assert _place(a1.id) == Decimal("3.75")
        assert str(_group_row(group.id).balance) in ("3.75", "3.7500")
        merged = (
            BottleLedger.query.filter(*BottleScope.for_group(group.id).ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        assert [e.balance_after for e in merged] == [Decimal("1.50"), Decimal("3.75")]

    def test_a_DRIFTED_address_carries_its_STORED_figure_and_is_NOT_repaired(self, db):
        """The dev DB's address-24 shape (stored 20.00, zero ledger rows), carried
        through a join. The single most valuable regression pin on this path: a
        refactor that rebuilt the place from ledger sums would silently destroy
        the 20 real bottles the row records."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _seed(db, a1, u1, "20")
        # Reproduce the drift: keep the row the service wrote, drop the entries.
        BottleLedger.query.filter_by(address_id=a1.id).delete(synchronize_session=False)
        db.session.commit()
        _deliver(db, u2, a2, "5")
        total_before = _stored_total()
        assert total_before == Decimal("25.00")

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )

        assert _place(a1.id) == Decimal("25.00")            # stored carried
        assert _group_ledger_sum(group.id) == Decimal("5.00")   # ledger unchanged
        assert _stored_total() == total_before
        # No repair happened: nothing in the merge_backfill namespace exists.
        assert BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("merge_backfill:%")
        ).count() == 0
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# B. The create fences
# =========================================================================== #


class TestCreateFences:
    """The fence CODES themselves (min-addresses, missing address, entity owner,
    already-grouped) and their "writes nothing" property are pinned by
    `test_place_conservation_invariants_e2e.py::test_every_join_fence_writes_absolutely_nothing`.
    What is added here is what that test cannot see: the ORDER of the two owner
    fences, the non-CUSTOMER ROLE shape, and the fact that a rejection survives
    an unrelated SUCCESSFUL commit on the same session."""

    def test_a_rejected_create_leaves_no_flushed_group_for_the_NEXT_commit_to_adopt(self, db):
        """The eligibility fence runs before `db.session.add(group)` deliberately.
        Validating after the flush would leave a half-built group that the next
        unrelated commit on the shared session silently persists — so the proof
        is an unrelated SUCCESSFUL commit right after the rejection."""
        place = _funded_place(db, "5", member_count=2)
        outsider_user = _user(db)
        outsider = _addr(db, outsider_user.id)
        third_user = _user(db)
        third = _addr(db, third_user.id)
        groups_before = AddressGroup.query.count()

        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group(
                [outsider.id, place.a.id], acting_admin_id=place.admin.id, reason="grab"
            )

        assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"
        assert AddressGroup.query.count() == groups_before
        assert outsider.address_group_id is None
        assert _place(place.a.id) == Decimal("5.00")

        # An unrelated successful commit on the SAME session.
        _seed(db, third, third_user, "2")

        assert AddressGroup.query.count() == groups_before
        assert outsider.address_group_id is None
        assert _place(place.a.id) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_grocery_owner_is_refused_by_the_GROCERY_code_not_the_ENTITY_one(self, db):
        """The two owner fences sit in ONE loop and the grocery check is FIRST —
        which is load-bearing, because `is_grocery_store` is derived from
        `user_type=ENTITY` + `entity_subtype=GROCERY_STORE`, so EVERY grocery owner
        also trips the entity arm. Reordering the two ifs silently changes the
        money-path fence code (`PLACE_GROUP_GROCERY_MEMBER` protects the
        corporate-contract mirror and is what the nightly sweep reports on) into
        the generic one."""
        admin = _admin(db)
        grocery = _user(
            db,
            user_type=UserType.ENTITY,
            entity_subtype=EntitySubtype.GROCERY_STORE,
            company_name="Corner Shop",
        )
        assert grocery.is_grocery_store is True
        clean_user = _user(db)
        a_grocery, a_clean = _addr(db, grocery.id), _addr(db, clean_user.id)
        before = _snapshot()

        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group(
                [a_grocery.id, a_clean.id], acting_admin_id=admin.id, reason="shop"
            )

        assert exc.value.error_code == "PLACE_GROUP_GROCERY_MEMBER"
        assert _snapshot() == before

    def test_a_non_CUSTOMER_role_is_refused_even_though_it_is_an_INDIVIDUAL(self, db):
        """The owner fence is a conjunction: individual AND customer. A driver's
        own (individual) address must not be poolable with a customer's."""
        admin = _admin(db)
        driver = _user(db, role=UserRole.DELIVERY_DRIVER)
        assert driver.user_type == UserType.INDIVIDUAL
        clean_user = _user(db)
        a_driver, a_clean = _addr(db, driver.id), _addr(db, clean_user.id)
        before = _snapshot()

        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group(
                [a_driver.id, a_clean.id], acting_admin_id=admin.id, reason="office"
            )

        assert exc.value.error_code == "PLACE_GROUP_ENTITY_MEMBER"
        assert _snapshot() == before
        assert a_clean.address_group_id is None


# =========================================================================== #
# B2. A join that fails HALFWAY THROUGH THE LOOP
# =========================================================================== #


class TestAJoinThatFailsMidLoop:
    """`_absorb_joiners_into_group` step 3 is a LOOP, and every iteration DELETEs
    a `bottle_balances` row and FLUSHes after handing its figure back to the
    caller in a Python local (`absorbed`). Between that flush and step 5's single
    credit, N bottles exist only in process memory. It is the widest data-loss
    window in the feature and it runs once per joining address.

    Every other "a rejection writes NOTHING" test in this file rejects BEFORE the
    first write — the fences all run ahead of `_absorb_joiners_into_group`. This
    class is the only one that fails the loop mid-way, which is the case where
    `_rollback_db_session()` in the route's bare `except Exception` is the sole
    thing standing between a 500 and half a place's bottles disappearing."""

    def test_a_join_that_dies_on_the_SECOND_absorb_leaves_every_deleted_row_restored(
        self, db, client, app, monkeypatch
    ):
        admin = _admin(db)
        ua, ub = _user(db), _user(db)
        a, b = _addr(db, ua.id), _addr(db, ub.id)
        _deliver(db, ua, a, "9")
        _give_back(db, ua, a, "2")                     # A: 7.00 across TWO own rows
        _deliver(db, ub, b, "3")                       # B: 3.00 across one
        bystander = _funded_place(db, "5", member_count=2)   # an unrelated place
        assert (_own_row(a.id).balance, _own_row(b.id).balance) == (
            Decimal("7.00"), Decimal("3.00")
        )
        before = _snapshot()
        absorbed_calls = []

        real_absorb = BottleTrackingService.absorb_address_into_group

        def _die_on_the_second_joiner(address_id, group_id):
            absorbed_calls.append(address_id)
            if len(absorbed_calls) == 1:
                return real_absorb(address_id, group_id)   # A really IS absorbed
            raise RuntimeError("boom, mid-loop")

        monkeypatch.setattr(
            BottleTrackingService,
            "absorb_address_into_group",
            staticmethod(_die_on_the_second_joiner),
        )

        response = client.post(
            "/api/v1/admin/place-groups",
            json={"addressIds": [a.id, b.id], "reason": "same office"},
            headers=_headers(app, admin),
        )

        assert response.status_code == 500, response.get_json()
        assert absorbed_calls == [a.id, b.id], "the loop did not reach the second joiner"
        # A's row was really DELETED and its ledger really re-stamped before the
        # crash; both are back, and back in the ADDRESS scope.
        assert _own_row(a.id) is not None
        assert _own_row(a.id).balance == Decimal("7.00")
        assert _own_ledger_sum(a.id) == Decimal("7.00")
        assert [
            e.address_group_id
            for e in BottleLedger.query.filter_by(address_id=a.id).all()
        ] == [None, None]
        assert _own_row(b.id).balance == Decimal("3.00")
        assert (
            UserAddress.query.get(a.id).address_group_id,
            UserAddress.query.get(b.id).address_group_id,
        ) == (None, None)
        assert _place(bystander.a.id) == Decimal("5.00")
        # No group, no audit event, no movement anywhere in the database.
        assert _snapshot() == before
        assert _sweep() == _CLEAN_SWEEP

        monkeypatch.undo()
        db.session.rollback()
        retry = client.post(
            "/api/v1/admin/place-groups",
            json={"addressIds": [a.id, b.id], "reason": "same office"},
            headers=_headers(app, admin),
        )

        assert retry.status_code == 201, retry.get_json()
        group_id = retry.get_json()["data"]["place_group_id"]
        assert _place(a.id) == _place(b.id) == Decimal("10.00")
        assert _own_row(a.id) is None and _own_row(b.id) is None
        assert _group_ledger_sum(group_id) == Decimal("10.00")
        assert _stored_total() == before["stored_total"] == Decimal("15.00")
        assert _place(bystander.a.id) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# C. The audit trail — `reason LIKE '[group N]%'` IS the scope key
# =========================================================================== #


class TestTheAuditTrail:
    def test_create_writes_exactly_one_event_with_deduplicated_owners(self, db):
        """`member_user_ids` is built from a set comprehension over `addresses`
        AFTER the absorb ran `expire_all()`; a lazy-refresh failure would produce
        duplicates or a stale owner id. Two addresses of ONE user plus a second
        user's address is the shape that shows it."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a1b, a2 = _addr(db, u1.id), _addr(db, u1.id, "Desk 2"), _addr(db, u2.id)
        events_before = CustomerLinkEvent.query.count()

        group = CustomerLinkService().create_place_group(
            [a1.id, a1b.id, a2.id], acting_admin_id=admin.id, reason="shared office 3F"
        )

        assert CustomerLinkEvent.query.count() == events_before + 1
        event = CustomerLinkEvent.query.order_by(CustomerLinkEvent.id.desc()).first()
        assert event.event_type == "create_place_group"
        assert event.canonical_customer_id is None
        assert event.acting_admin_id == admin.id
        assert event.member_user_ids == sorted({u1.id, u2.id})
        assert event.reason == f"[group {group.id}] shared office 3F"
        assert event.event_metadata["rescoped_ledger_entry_ids"] == []
        trail = _events(group.id)
        assert [e["id"] for e in trail] == [event.id]

    def test_an_empty_reason_still_leaves_a_findable_event(self, db, client, admin_auth_headers):
        """The LEGACY route passes `reason=''`, and `f"[group {id}] ".strip()`
        drops the trailing space — so the stored value is exactly "[group N]".
        The events filter is `'[group N]%'`, which still matches; tightening it
        to `'[group N] %'` would make the entire legacy route's audit trail
        vanish. (It also means an unaccountable place-group creation is reachable
        over HTTP today — reported, not asserted away.)"""
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)

        response = client.post(
            "/api/v1/admin/canonical-customers/424242/address-groups",
            json={"addressIds": [a1.id, a2.id], "label": "3F"},
            headers=admin_auth_headers,
        )

        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["address_group_id"]
        assert response.get_json()["data"]["address_ids"] == sorted([a1.id, a2.id])
        event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
        assert event.reason == f"[group {group_id}]"
        assert [e["id"] for e in _events(group_id)] == [event.id]

    def test_the_legacy_route_builds_an_ownerless_place_that_absorbs_balances(self, db, client,
                                                                             admin_auth_headers):
        """Two live create routes with different validation is a drift surface —
        so the legacy one must at least produce the same PLACE."""
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _deliver(db, u1, a1, "6")
        _deliver(db, u2, a2, "5")
        total_before = _stored_total()

        response = client.post(
            "/api/v1/admin/canonical-customers/7/address-groups",
            json={"addressIds": [a1.id, a2.id], "reason": "legacy path"},
            headers=admin_auth_headers,
        )

        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["address_group_id"]
        group = AddressGroup.query.get(group_id)
        assert group.canonical_customer_id is None          # ownerless, per spec §9
        assert _place(a1.id) == Decimal("11.00")
        assert BottleBalance.query.count() == 1
        assert _stored_total() == total_before
        assert _sweep() == _CLEAN_SWEEP

    def test_a_900_character_reason_is_truncated_without_losing_the_prefix_or_the_marker(self, db):
        """`reason` is String(500): the create path slices AFTER prefixing, and the
        dissolve re-slices to `500 - len(marker)` and appends. An off-by-one
        either overflows the column or eats the marker."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        _seed(db, a1, u1, "4")

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="x" * 900
        )
        create_event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
        assert len(create_event.reason) == 500
        assert create_event.reason.startswith(f"[group {group.id}] ")

        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="y" * 900
        )

        remove_event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        marker = " | place dissolved onto its last member"
        assert len(remove_event.reason) == 500
        assert remove_event.reason.endswith(marker)
        assert remove_event.reason.startswith(f"[group {group.id}] ")
        assert {e["id"] for e in _events(group.id)} == {create_event.id, remove_event.id}

    def test_one_groups_trail_never_leaks_into_a_group_whose_id_it_PREFIXES(self, db):
        """The whole audit scoping rests on a LIKE prefix rather than a column.
        The closing bracket is what stops `'[group 1]%'` matching
        `'[group 11] ...'` — dropping it merges two places' histories."""
        admin = _admin(db)
        u1, u2, u3 = _user(db), _user(db), _user(db)
        created = []
        for _ in range(11):
            pair = [_addr(db, u1.id).id, _addr(db, u2.id).id]
            created.append(
                CustomerLinkService().create_place_group(
                    pair, acting_admin_id=admin.id, reason="office"
                ).id
            )
        pairs = [
            (short, long)
            for short in created
            for long in created
            if short != long and str(long).startswith(str(short))
        ]
        assert pairs, f"no prefixing pair of group ids among {created}"
        short_id, long_id = pairs[0]

        # Give the long-id group two more events so a leak would be visible.
        extra = _addr(db, u3.id)
        CustomerLinkService().add_addresses_to_group(
            long_id, [extra.id], acting_admin_id=admin.id, reason="moved in"
        )
        CustomerLinkService().remove_address_from_group(
            extra.id, acting_admin_id=admin.id, reason="moved out"
        )

        short_trail, long_trail = _events(short_id), _events(long_id)
        assert len(short_trail) == 1
        assert len(long_trail) == 3
        assert all(e["reason"].startswith(f"[group {short_id}] ") for e in short_trail)
        assert all(e["reason"].startswith(f"[group {long_id}] ") for e in long_trail)
        assert not {e["id"] for e in short_trail} & {e["id"] for e in long_trail}

    def test_a_removal_that_CRASHES_MID_SPLIT_records_NO_event_and_BURNS_its_episode_id(
        self, db, client, app, monkeypatch
    ):
        """An audit trail that records a removal which never happened.

        `remove_address_from_group` `add()`s and FLUSHes the `CustomerLinkEvent`
        BEFORE the split runs — deliberately, because `event.id` is the episode
        handle every `place_leave:{group}:{event}:{addr}:out|in` key is built
        from. So between that flush and the commit there is a window where the
        audit row exists and the bottles have not moved, and only the
        transaction boundary closes it. Every audit consumer
        (`get_place_group_events`, the panel's history) reads that table as the
        record of what happened, and the happy path is all TestTheAuditTrail
        asserts above — a removal that rolled back but stayed on the trail would
        pass the entire suite while telling a manager somebody left a place they
        never left.

        The crash is aimed at the `:in` half specifically: the `:out` half has
        already flushed by then, so this is a genuinely half-written split, not
        a failure before the first write.

        The second half of the property matters as much as the first. The failed
        attempt already built (and flushed) a `place_leave:` key from its episode
        id, and `_create_ledger_entry`'s idempotency lookup is GLOBAL by key: if
        one survived, the retry would be silently short-circuited as a duplicate
        and move no bottles at all. So the retry is asserted to have really
        MOVED the two crates, not merely to have returned 200.

        The stronger-sounding property "the retry gets a DIFFERENT episode id"
        is deliberately NOT asserted: `customer_link_events.id` is a SQLite
        rowid alias here, and a rolled-back insert releases it again, so the
        retry legitimately reuses it on this database and would not on Postgres.
        What actually protects the retry is that the burned attempt left NO key
        behind — which is asserted directly, and is true on both."""
        place = _funded_place(db, "9", member_count=3)
        events_before = CustomerLinkEvent.query.count()
        before = _snapshot()
        seen_keys = []

        real_entry = BottleTrackingService._create_ledger_entry

        def _die_on_the_in_half(self, **kwargs):
            key = kwargs.get("idempotency_key") or ""
            seen_keys.append(key)
            if key.endswith(":in"):
                raise RuntimeError("boom, mid-split")
            return real_entry(self, **kwargs)

        monkeypatch.setattr(BottleTrackingService, "_create_ledger_entry", _die_on_the_in_half)

        response = client.delete(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
            json={"reason": "left the office", "bottlesLeaving": 2},
            headers=_headers(app, place.admin),
        )

        assert response.status_code == 500, response.get_json()
        # The `:out` half really did flush before the crash, so the episode id it
        # keyed itself to is knowable — and is the one that must never come back.
        out_keys = [k for k in seen_keys if k.endswith(":out")]
        assert out_keys, seen_keys
        burned_event_id = int(out_keys[0].split(":")[2])
        assert CustomerLinkEvent.query.count() == events_before, (
            "the flushed-first audit event survived a rolled-back removal"
        )
        assert UserAddress.query.get(place.a.id).address_group_id == place.group_id
        assert _place(place.a.id) == Decimal("9.00")
        # Nothing from the burned episode survives for the GLOBAL key lookup to find.
        assert _keys_like("place_leave:%") == []
        assert _keys_like(f"place_leave:%:{burned_event_id}:%") == []
        assert _snapshot() == before
        assert _sweep() == _CLEAN_SWEEP

        monkeypatch.undo()
        retry = client.delete(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
            json={"reason": "left the office", "bottlesLeaving": 2},
            headers=_headers(app, place.admin),
        )

        assert retry.status_code == 200, retry.get_json()
        assert retry.get_json()["data"]["dissolved"] is False
        assert CustomerLinkEvent.query.count() == events_before + 1
        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        assert _keys_like("place_leave:%") == sorted(
            [
                f"place_leave:{place.group_id}:{event.id}:{place.a.id}:in",
                f"place_leave:{place.group_id}:{event.id}:{place.a.id}:out",
            ]
        )
        # The retry MOVED bottles — it was not short-circuited as a duplicate.
        assert (_place(place.b.id), _place(place.a.id)) == (Decimal("7.00"), Decimal("2.00"))
        assert _stored_total() == before["stored_total"] == Decimal("9.00")
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# D. Joining an existing place
# =========================================================================== #


class TestJoinAnExistingPlace:
    def test_a_funded_joiner_is_absorbed_and_audited_as_its_own_episode(self, db):
        place = _funded_place(db, "11", member_count=2)
        joiner_user = _user(db)
        joiner = _addr(db, joiner_user.id)
        entry = _deliver(db, joiner_user, joiner, "4")
        total_before = _stored_total()

        place.add([joiner], reason="moved in")

        assert _place(place.a.id) == _place(joiner.id) == Decimal("15.00")
        assert _stored_total() == total_before
        assert _own_row(joiner.id) is None
        db.session.refresh(entry)
        assert entry.address_group_id == place.group_id
        event = CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").one()
        assert event.member_user_ids == [joiner_user.id]
        assert event.event_metadata["rescoped_ledger_entry_ids"] == [entry.id]
        assert event.reason == f"[group {place.group_id}] moved in"
        assert _sweep() == _CLEAN_SWEEP

    def test_a_historyless_joiner_changes_nothing_and_creates_no_row(self, db):
        place = _funded_place(db, "7", member_count=2)
        joiner = _addr(db, _user(db).id)
        row_before = _group_row(place.group_id)
        balance_before, rows_before = row_before.balance, BottleBalance.query.count()

        place.add([joiner])

        assert _group_row(place.group_id).balance == balance_before == Decimal("7.00")
        assert BottleBalance.query.count() == rows_before
        assert _own_row(joiner.id) is None
        event = CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").one()
        assert event.event_metadata["rescoped_ledger_entry_ids"] == []
        assert _sweep() == _CLEAN_SWEEP

    def test_joining_a_place_with_NO_balance_row_creates_exactly_one_late(self, db):
        """The ONE two-row acquirer that takes the group row LAST, and the only
        path that inserts a group row through `get_or_create_balance`'s
        ON CONFLICT DO NOTHING. A scope/conflict-column mismatch would either
        insert a second row or silently no-op leaving the place at 0."""
        place = _Place(db, member_count=2)                 # historyless -> no group row
        assert _group_row(place.group_id) is None
        first_user, second_user = _user(db), _user(db)
        first, second = _addr(db, first_user.id), _addr(db, second_user.id)
        _deliver(db, first_user, first, "6")
        _deliver(db, second_user, second, "2")
        total_before = _stored_total()

        place.add([first])

        row = _group_row(place.group_id)
        assert row is not None and row.address_id is None and row.balance == Decimal("6.00")
        assert BottleBalance.query.filter_by(address_group_id=place.group_id).count() == 1
        BottleTrackingService.assert_scope_row_valid(row)

        place.add([second], reason="and another")          # a SECOND join must reuse it

        assert BottleBalance.query.filter_by(address_group_id=place.group_id).count() == 1
        assert _place(place.a.id) == Decimal("8.00")
        assert _stored_total() == total_before
        assert _sweep() == _CLEAN_SWEEP

    def test_the_join_rewrites_only_the_scope_and_the_snapshot(self, db):
        """The bulk UPDATE runs with `synchronize_session=False` followed by
        `expire_all()`. Every immutable fact on every re-stamped entry must be
        byte-identical afterwards; only `address_group_id` and `balance_after`
        may move."""
        place = _funded_place(db, "5", member_count=2)
        joiner_user = _user(db)
        joiner = _addr(db, joiner_user.id)
        delivered = _deliver(db, joiner_user, joiner, "3")
        returned = _give_back(db, joiner_user, joiner, "1")
        adjusted = _seed(db, joiner, joiner_user, "2", notes="stock count")
        before = {e.id: _entry_facts(e) for e in (delivered, returned, adjusted)}
        total_before = _stored_total()

        place.add([joiner])

        for entry_id, facts in before.items():
            entry = BottleLedger.query.get(entry_id)
            assert _entry_facts(entry) == facts, f"entry {entry_id} lost an immutable fact"
            assert entry.address_group_id == place.group_id
        assert _place(joiner.id) == _place(place.a.id) == Decimal("9.00")
        assert _stored_total() == total_before == Decimal("9.00")   # the pair
        assert _own_row(joiner.id) is None
        assert _group_ledger_sum(place.group_id) == Decimal("9.00")

    def test_snapshots_walk_the_merged_timeline_and_are_STABLE_across_reruns(self, db):
        """Ordering by `occurred_at` alone is non-deterministic for the paired
        writes that share a timestamp; the `.id` tie-break is what makes the
        history view stop flipping between runs."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        first = _deliver(db, u1, a1, "6")
        second = _deliver(db, u2, a2, "5")
        third = _give_back(db, u2, a2, "4")
        # Two entries sharing ONE occurred_at (what a paired write looks like).
        shared_ts = datetime.now(UTC) - timedelta(minutes=5)
        for entry in (first, second):
            entry.occurred_at = shared_ts
        third.occurred_at = shared_ts + timedelta(minutes=1)
        db.session.commit()

        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )
        scope = BottleScope.for_group(group.id)

        def timeline():
            rows = (
                BottleLedger.query.filter(*scope.ledger_filter())
                .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
                .all()
            )
            return [(r.id, r.quantity, r.balance_after) for r in rows]

        first_pass = timeline()
        assert [q for _, q, _ in first_pass] == [
            Decimal("6.00"), Decimal("5.00"), Decimal("-4.00")
        ]
        assert [b for _, _, b in first_pass] == [
            Decimal("6.00"), Decimal("11.00"), Decimal("7.00")
        ]
        assert first_pass[-1][2] == _place(a1.id) == Decimal("7.00")

        BottleTrackingService.recompute_balance_after(scope)
        db.session.commit()

        assert timeline() == first_pass


# =========================================================================== #
# E. The join fences
# =========================================================================== #


class TestJoinFences:
    def test_an_address_in_ANOTHER_place_is_refused_and_both_places_are_untouched(self, db):
        """The fence runs before the absorb, and the absorb's FIRST step is the
        membership pointer write. Moving the fence after it would re-stamp the
        other place's rows before raising."""
        source = _funded_place(db, "10", member_count=2)
        target = _funded_place(db, "4", member_count=2)
        before = _snapshot()

        with pytest.raises(ValidationError) as exc:
            target.add([source.a])

        assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"
        assert str(source.a.id) in exc.value.message
        assert _snapshot() == before
        assert _place(source.a.id) == Decimal("10.00")
        assert _place(target.a.id) == Decimal("4.00")
        assert CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").count() == 0

    def test_an_address_already_in_THIS_place_is_refused_not_silently_idempotent(self, db):
        place = _funded_place(db, "9", member_count=3)
        before = _snapshot()

        for _ in range(2):
            with pytest.raises(ValidationError) as exc:
                place.add([place.b])
            assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

        assert _snapshot() == before
        assert _place(place.a.id) == Decimal("9.00")

    def test_an_empty_address_list_is_a_no_op_on_the_service_and_a_400_on_the_route(
        self, db, client, admin_auth_headers
    ):
        """The early return sits BEFORE `_load_addresses`; a reorder would still
        write an audit event for a no-op."""
        place = _funded_place(db, "5", member_count=2)
        before = _snapshot()

        returned = place.add([])

        assert returned.id == place.group_id
        assert _snapshot() == before

        response = client.post(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses",
            json={"addressIds": [], "reason": "nothing"},
            headers=admin_auth_headers,
        )

        assert response.status_code == 400
        assert response.get_json()["errors"] == ["addressIds is required"]
        assert _snapshot() == before

    def test_a_missing_group_is_a_404_on_the_route_and_a_named_code_on_the_service(
        self, db, client, admin_auth_headers
    ):
        a1 = _addr(db, _user(db).id)
        admin = _admin(db)

        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().add_addresses_to_group(
                999_999, [a1.id], acting_admin_id=admin.id, reason="office"
            )
        assert exc.value.error_code == "PLACE_GROUP_NOT_FOUND"

        response = client.post(
            "/api/v1/admin/place-groups/999999/addresses",
            json={"addressIds": [a1.id], "reason": "office"},
            headers=admin_auth_headers,
        )

        assert response.status_code == 404, response.get_json()
        # `not_found_response(resource_type=...)` renders the type INTO the message.
        assert response.get_json()["message"] == "PlaceGroup not found"
        assert a1.address_group_id is None


# =========================================================================== #
# F. Re-adding a departed address
# =========================================================================== #


class TestReAddingADepartedAddress:
    def test_re_adding_a_split_out_member_makes_the_place_read_the_FULL_total_again(self, db):
        """The bug §7.2 closed. If the absorb's IS-NULL selector or the balance
        carry regresses, the split bottles are stranded in a row nothing resolves
        to and the place under-reports by exactly the split."""
        place = _funded_place(db, "10", member_count=3)
        total_before = _stored_total()

        place.remove(place.a, reason="left with four", bottles_leaving=4)
        assert (_place(place.b.id), _place(place.a.id)) == (Decimal("6.00"), Decimal("4.00"))

        place.add([place.a], reason="came back")

        assert _place(place.b.id) == _place(place.a.id) == Decimal("10.00")
        assert _own_row(place.a.id) is None
        assert _stored_total() == total_before
        # The ':in' half followed the address back in; both figures agree again.
        in_half = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"place_leave:{place.group_id}:%:in")
        ).one()
        assert in_half.address_group_id == place.group_id
        assert _group_ledger_sum(place.group_id) == Decimal("10.00") == _place(place.a.id)
        assert _sweep() == _CLEAN_SWEEP

    def test_a_re_join_cannot_capture_the_FORMER_places_rows(self, db):
        """`address_id = a AND address_group_id IS NULL` — dropping the IS NULL arm
        (an easy "simplification") drags an entire other place's history into the
        new group, minting bottles there and erasing them at the old place."""
        old = _funded_place(db, "10", member_count=3)
        new = _funded_place(db, "4", member_count=2)
        old_entry_ids = {
            e.id for e in BottleLedger.query.filter_by(address_group_id=old.group_id).all()
        }
        assert old_entry_ids
        total_before = _stored_total()

        old.remove(old.a, reason="left with nothing")      # default: bottles stay
        assert _place(old.a.id) == Decimal("0.00")

        new.add([old.a], reason="joined the other office")

        assert _place(new.a.id) == Decimal("4.00")         # gained NOTHING
        assert _place(old.b.id) == Decimal("10.00")        # lost nothing
        assert _stored_total() == total_before
        for entry_id in old_entry_ids:
            assert BottleLedger.query.get(entry_id).address_group_id == old.group_id
        assert _group_ledger_sum(new.group_id) == Decimal("4.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_joiner_carrying_place_leave_and_place_dissolve_halves_is_absorbed_whole(self, db):
        """The absorb is key-blind by design. A key-prefix exclusion here
        (mirroring `suggested_bottles_leaving`'s place-level prefixes) would make
        the carried balance and the re-stamped ledger disagree instantly."""
        # 1. A split gives the address a place_leave ':in' of +4.
        split_source = _funded_place(db, "10", member_count=3)
        traveller, traveller_user = split_source.a, split_source.ua
        split_source.remove(traveller, reason="took four", bottles_leaving=4)
        # 2. A dissolve gives it a place_dissolve ':in' of +6.
        dissolving = _Place(db, member_count=2)
        _seed(db, dissolving.b, dissolving.ub, "6")
        dissolving.svc.add_addresses_to_group(
            dissolving.group_id, [traveller.id], acting_admin_id=dissolving.admin.id,
            reason="second office",
        )
        dissolving.remove(dissolving.a, reason="a leaves")
        dissolving.remove(dissolving.b, reason="b leaves")     # dissolves onto traveller
        # 3. Plus a plain delivery of +2.
        _deliver(db, traveller_user, traveller, "2")
        db.session.expire_all()
        assert _place(traveller.id) == Decimal("12.00")
        own_entries = BottleLedger.query.filter(
            BottleLedger.address_id == traveller.id,
            BottleLedger.address_group_id.is_(None),
        ).all()
        # Exactly one ':in' half of each kind, with the quantity each one carried —
        # a set comprehension collapsing to {"place_leave"} would pass on any
        # number of them, including a duplicated pair.
        by_prefix = {
            prefix: [e for e in own_entries if (e.idempotency_key or "").startswith(prefix)]
            for prefix in ("place_leave", "place_dissolve")
        }
        assert [e.quantity for e in by_prefix["place_leave"]] == [Decimal("4.00")]
        assert [e.quantity for e in by_prefix["place_dissolve"]] == [Decimal("6.00")]
        assert all(
            e.idempotency_key.endswith(":in")
            for e in by_prefix["place_leave"] + by_prefix["place_dissolve"]
        )
        assert len(own_entries) == 3            # ...plus the plain delivery of 2
        assert _own_ledger_sum(traveller.id) == Decimal("12.00")
        target = _funded_place(db, "3", member_count=2)
        total_before = _stored_total()

        target.add([traveller], reason="third office")

        assert _place(target.a.id) == Decimal("15.00")         # 3 + 12, exactly
        assert _stored_total() == total_before
        assert _own_row(traveller.id) is None
        for entry in own_entries:
            assert BottleLedger.query.get(entry.id).address_group_id == target.group_id
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# G. Leaving
# =========================================================================== #


class TestLeaving:
    def test_the_removal_event_names_ONLY_the_departing_owner(self, db):
        """The audit trail is filtered by the reason prefix, not by member ids,
        precisely so this event — whose owner is no longer a member — survives."""
        place = _funded_place(db, "9", member_count=3)
        create_event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
        created_members = list(create_event.member_user_ids)

        place.remove(place.a, reason="left the office")

        remove_event = CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group"
        ).one()
        assert remove_event.member_user_ids == [place.ua.id]
        assert remove_event.reason == f"[group {place.group_id}] left the office"
        db.session.refresh(create_event)
        assert create_event.member_user_ids == created_members
        assert {e["id"] for e in _events(place.group_id)} == {create_event.id, remove_event.id}

    def test_a_replayed_removal_is_refused_and_never_double_splits(self, db):
        """The idempotency keys embed the event id, so a replay that reached the
        split would write a SECOND pair under a different key and double it. The
        membership check is the only guard."""
        place = _funded_place(db, "10", member_count=3)

        place.remove(place.a, reason="took four", bottles_leaving=4)
        after_first = _snapshot()

        with pytest.raises(ValidationError) as exc:
            place.remove(place.a, reason="took four", bottles_leaving=4)

        assert exc.value.error_code == "PLACE_GROUP_NOT_FOUND"
        assert _snapshot() == after_first
        assert CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").count() == 1
        assert len(_leave_rows(place.a.id)) == 2
        assert (_place(place.a.id), _place(place.b.id)) == (Decimal("4.00"), Decimal("6.00"))

    def test_remove_then_readd_then_remove_on_the_default_path_writes_NO_adjustment(self, db):
        """§8 retired netting: the default removal and the re-join are inverse
        NO-OPS. Any "settle up on removal" logic reintroduces it."""
        place = _funded_place(db, "10", member_count=4)
        adjustments_before = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).count()
        total_before = _stored_total()

        place.remove(place.a, reason="left")
        assert _place(place.b.id) == Decimal("10.00")
        place.add([place.a], reason="came back")
        assert _place(place.b.id) == Decimal("10.00")
        place.remove(place.a, reason="left again")

        assert _place(place.b.id) == Decimal("10.00")
        assert _own_row(place.a.id) is None
        assert _place(place.a.id) == Decimal("0.00")
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).count() == adjustments_before
        assert _stored_total() == total_before
        assert _group_ledger_sum(place.group_id) == Decimal("10.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_delivery_that_already_landed_at_the_place_STAYS_with_the_place(self, db):
        """The gap between what the dialog OFFERS and what the API DEFAULTS to is
        exactly where an admin loses bottles: the suggestion is 6, the default is
        0, and the default must win."""
        place = _funded_place(db, "0", member_count=3)
        _deliver(db, place.ua, place.a, "6")
        assert BottleTrackingService.suggested_bottles_leaving(
            place.group_id, place.a.id
        ) == Decimal("6.00")
        total_before = _stored_total()

        result = place.remove(place.a, reason="left")

        assert result["bottles_leaving"] == Decimal("0.00")
        assert "netting" not in result
        assert _place(place.b.id) == Decimal("6.00")
        assert _place(place.a.id) == Decimal("0.00")
        assert _stored_total() == total_before
        assert _leave_rows(place.a.id) == []


# =========================================================================== #
# H. Dissolving (the transitions the sibling dissolve file does not walk)
# =========================================================================== #


class TestDissolveLifecycle:
    def test_the_dissolve_metadata_carries_typed_audit_values_on_ONE_episode(self, db):
        """`dissolved_inherited_bottles` is a STRING, not a float: `float(5)` in an
        audit record becomes 4.999999999999999 on the wrong quantity.

        The SURVIVOR is given history of its own on purpose. With bottles only at
        the leaver's address `dissolved_rescoped_ledger_entry_ids` comes back
        EMPTY — `release_group_history_to_address` re-stamps only the survivor's
        own entries — and every assertion about that list (sorted, all-ints,
        contents) is then vacuously true. Two entries at the survivor and one at
        the leaver make the ids assertable and keep `inherited` non-trivial: the
        place holds 9, the survivor's own history explains 2 of it, so exactly 7
        cross as the paired adjustment."""
        place = _Place(db, member_count=2)
        _seed(db, place.a, place.ua, "7")             # attributed to the LEAVER
        survivor_in = _deliver(db, place.ub, place.b, "3")
        survivor_back = _give_back(db, place.ub, place.b, "1")
        assert _place(place.a.id) == Decimal("9.00")
        events_before = CustomerLinkEvent.query.count()

        result = place.remove(place.a, reason="moved out")

        assert result["dissolved"] is True
        assert CustomerLinkEvent.query.count() == events_before + 1
        event = CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").one()
        meta = event.event_metadata
        assert meta["dissolved_onto_address_id"] == place.b.id
        assert meta["dissolved_inherited_bottles"] == "7.00"
        assert isinstance(meta["dissolved_inherited_bottles"], str)
        rescoped = meta["dissolved_rescoped_ledger_entry_ids"]
        assert isinstance(rescoped, list)
        assert rescoped == sorted([survivor_in.id, survivor_back.id])
        assert all(isinstance(i, int) for i in rescoped)
        assert [e["id"] for e in _events(place.group_id)][0] == event.id
        # ...and the numbers behind the audit record are the ones it claims.
        assert _place(place.b.id) == Decimal("9.00")
        assert _own_ledger_sum(place.b.id) == Decimal("9.00")
        assert _group_ledger_sum(place.group_id) == Decimal("0.00")

    def test_a_drifted_dissolve_leaves_the_drift_in_the_GROUPS_ledger_only(self, db):
        """The survivor ends holding the STORED figure and its own ledger explains
        it exactly; the unexplained remainder stays behind as the group's
        leftover ledger, summing to -drift with its own recomputed snapshots."""
        place = _Place(db, member_count=2)
        _seed(db, place.a, place.ua, "20")
        _drop_ledger_for_group(db, place.group_id)         # the address-24 shape
        assert _place(place.a.id) == Decimal("20.00")
        assert _group_ledger_sum(place.group_id) == Decimal("0.00")
        total_before = _stored_total()

        place.remove(place.a, reason="moved out")

        assert _place(place.b.id) == Decimal("20.00")
        assert _stored_total() == total_before == Decimal("20.00")
        assert _own_ledger_sum(place.b.id) == Decimal("20.00")     # survivor converged
        assert _group_ledger_sum(place.group_id) == Decimal("-20.00")   # the drift
        assert _group_row(place.group_id) is None
        # The group's leftover timeline was recomputed too, not left stale.
        leftovers = (
            BottleLedger.query.filter(*BottleScope.for_group(place.group_id).ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        assert [e.balance_after for e in leftovers] == [Decimal("-20.00")]
        assert _sweep() == _CLEAN_SWEEP

    def test_a_dissolve_that_CRASHES_BEFORE_THE_GROUP_ROW_DELETE_restores_the_UNEXPLAINED_drift(
        self, db, client, app, monkeypatch
    ):
        """The one crash-and-resume case where ROLLBACK is the only recovery,
        because reconstruction is provably impossible.

        `release_group_history_to_address` is five ordered steps on a place whose
        figure CANNOT be rebuilt from its ledger: it carries `own_sum` across two
        rows, bulk re-stamps a ledger, appends a paired zero-sum adjustment for
        the unexplained remainder, `expire_all()`s twice and finally DELETEs the
        group's row. Here the place holds a STORED 20.00 that only 6.00 of ledger
        explains — the dev address-24 shape — so 14 of it exists in exactly one
        row and nowhere else. If the middle of that sequence half-lands, the 14
        is gone permanently and the next `reconcile_balance` press rebuilds the
        survivor at 6.00.

        The crash is placed on the SECOND `recompute_balance_after`: after the
        carry, after the paired adjustment, and immediately before
        `DELETE FROM bottle_balances WHERE address_group_id = g` — the last
        moment at which the group's row still holds the only copy of the drift."""
        place = _Place(db, member_count=2)
        _deliver(db, place.ub, place.b, "6")           # the survivor's own history
        _seed(db, place.a, place.ua, "14")
        # Only LEDGER rows go: the balance row stays the one the service wrote,
        # so the place stores 20.00 that its ledger explains 6.00 of (drift 14).
        _drop_ledger_for_member(db, place.group_id, place.a.id)
        assert _place(place.a.id) == Decimal("20.00")
        assert _group_ledger_sum(place.group_id) == Decimal("6.00")
        before = _snapshot()
        calls, burned = [], {}

        real_recompute = BottleTrackingService.recompute_balance_after

        def _die_before_the_group_row_delete(scope):
            calls.append(scope)
            if len(calls) == 2:
                burned["event_id"] = (
                    CustomerLinkEvent.query.order_by(CustomerLinkEvent.id.desc()).first().id
                )
                raise RuntimeError("boom, mid-dissolve")
            return real_recompute(scope)

        monkeypatch.setattr(
            BottleTrackingService,
            "recompute_balance_after",
            staticmethod(_die_before_the_group_row_delete),
        )

        response = client.delete(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
            json={"reason": "moved out"},
            headers=_headers(app, place.admin),
        )

        assert response.status_code == 500, response.get_json()
        assert len(calls) == 2, "the crash did not land between the two snapshot passes"
        # The unexplained 14 is BACK, in the one row that ever held it.
        assert _group_row(place.group_id).balance == Decimal("20.00")
        assert _own_row(place.b.id) is None
        assert sorted(
            a.id for a in UserAddress.query.filter_by(address_group_id=place.group_id)
        ) == sorted([place.a.id, place.b.id])
        assert _group_ledger_sum(place.group_id) == Decimal("6.00")
        assert _own_ledger_sum(place.b.id) == Decimal("0.00")
        # Nothing from the burned episode survives for the GLOBAL key lookup to
        # find, so the retry below cannot be short-circuited as a duplicate.
        assert _keys_like("place_dissolve:%") == []
        assert _keys_like(f"place_dissolve:%:{burned['event_id']}:%") == []
        assert _snapshot() == before
        assert _sweep() == _CLEAN_SWEEP

        monkeypatch.undo()
        retry = client.delete(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
            json={"reason": "moved out"},
            headers=_headers(app, place.admin),
        )

        assert retry.status_code == 200, retry.get_json()
        assert retry.get_json()["data"]["dissolved"] is True
        assert _place(place.b.id) == Decimal("20.00")           # the drift survived intact
        assert _stored_total() == before["stored_total"] == Decimal("20.00")
        assert _own_ledger_sum(place.b.id) == Decimal("20.00")
        assert _group_ledger_sum(place.group_id) == Decimal("-14.00")
        assert _group_row(place.group_id) is None
        retry_event = CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group"
        ).one()
        assert _keys_like("place_dissolve:%") == sorted(
            [
                f"place_dissolve:{place.group_id}:{retry_event.id}:in",
                f"place_dissolve:{place.group_id}:{retry_event.id}:out",
            ]
        )
        assert _sweep() == _CLEAN_SWEEP

    def test_the_survivors_own_row_is_DESTROYED_by_the_join_and_REBUILT_by_the_dissolve(self, db):
        """A round trip on the survivor's OWN `bottle_balances` row, asserted on
        the SCOPE SHAPE of every row in the table at each end.

        The row the survivor had before the join is not "topped up" later — the
        absorb DELETES it and hands its figure to the group, and the dissolve
        then makes a fresh address-keyed row via
        `release_group_history_to_address` -> `get_or_create_balance`. Asserting
        only `len(rows) == 1` at the end cannot tell a rebuilt row from an old
        one that was never absorbed, and the difference is the whole §7.2/§7.3
        contract: a join that left the old row behind strands it (invisible to
        every place-scoped read) while the place counts the same bottles again.
        So the mid-point is asserted too — while the place exists, NO
        address-keyed row may exist anywhere.

        Row IDs are deliberately not used as the discriminator: SQLite reuses
        rowids, so the group row inherits the deleted address row's id and an
        `id != original` assertion would fail for a reason that has nothing to
        do with the behaviour. `uq_bottle_balance_addr` IS enforced on SQLite,
        so a duplicate would still surface here."""
        # The survivor earns an address-keyed row BEFORE it ever joins a place.
        survivor_user = _user(db)
        survivor = _addr(db, survivor_user.id)
        _deliver(db, survivor_user, survivor, "2")
        leaver_user = _user(db)
        leaver = _addr(db, leaver_user.id)
        _deliver(db, leaver_user, leaver, "5")
        admin = _admin(db)
        total_before = _stored_total()
        assert _own_row(survivor.id).balance == Decimal("2.00")

        group = CustomerLinkService().create_place_group(
            [survivor.id, leaver.id], acting_admin_id=admin.id, reason="office"
        )
        assert _own_row(survivor.id) is None                # absorbed on the join
        # ...and DELETED, not stranded: while the place exists the table holds
        # exactly one row and it is the group's.
        assert [
            (r.address_id, r.address_group_id, r.balance) for r in BottleBalance.query.all()
        ] == [(None, group.id, Decimal("7.00"))]
        assert _place(survivor.id) == Decimal("7.00")

        CustomerLinkService().remove_address_from_group(
            leaver.id, acting_admin_id=admin.id, reason="moved out"
        )

        assert [
            (r.address_id, r.address_group_id, r.balance) for r in BottleBalance.query.all()
        ] == [(survivor.id, None, Decimal("7.00"))]
        BottleTrackingService.assert_scope_row_valid(BottleBalance.query.one())
        assert _stored_total() == total_before == Decimal("7.00")
        # Both members' entries came back out with the address, so the survivor's
        # own ledger explains its own figure exactly.
        assert _own_ledger_sum(survivor.id) == Decimal("7.00")
        assert _group_row(group.id) is None
        assert _group_ledger_sum(group.id) == Decimal("0.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_dissolved_place_CANNOT_be_repopulated_so_it_dissolves_exactly_ONCE(self, db):
        """UPDATED: this used to drive a group through TWO dissolve cycles.

        It existed because `place_dissolve:{group}:{event}:out` is group+EPISODE
        keyed, and narrowing the key to `{group}:out` would make a SECOND
        dissolve short-circuit on the FIRST entry, move no balance, and leave the
        group row non-zero — which the unconditional delete at the end would then
        DESTROY. A second cycle needed the dissolved group to be re-populated.

        That door is closed: a memberless group is refused as a join target
        (`PLACE_GROUP_DISSOLVED`), so a group dissolves exactly ONCE and the
        episode component of the key is now belt-and-braces rather than
        load-bearing. The key shape is still pinned — narrowing it would be a
        silent regression the day a forwarding/incarnation design re-opens
        re-tenanting — and the refusal is pinned alongside it.
        """
        place = _Place(db, member_count=2)
        _seed(db, place.a, place.ua, "7")               # attributed to the leaver
        total_before = _stored_total()

        place.remove(place.a, reason="first cycle")
        assert _place(place.b.id) == Decimal("7.00")
        assert UserAddress.query.filter_by(address_group_id=place.group_id).count() == 0

        third_user = _user(db)
        third = _addr(db, third_user.id)
        _deliver(db, third_user, third, "3")
        _assert_refused(
            lambda: place.add([place.b, third], reason="second cycle"),
            "PLACE_GROUP_DISSOLVED",
        )
        db.session.expire_all()

        keys = sorted(
            e.idempotency_key
            for e in BottleLedger.query.filter(
                BottleLedger.idempotency_key.like("place_dissolve:%")
            ).all()
        )
        assert len(keys) == len(set(keys))
        assert len({k.split(":")[2] for k in keys}) == 1, keys   # ONE episode, for ever
        assert all(k.startswith(f"place_dissolve:{place.group_id}:") for k in keys), keys
        assert _place(place.b.id) == Decimal("7.00")
        assert _place(third.id) == Decimal("3.00")
        assert _stored_total() == total_before + Decimal("3.00")
        assert _group_row(place.group_id) is None
        assert _sweep() == _CLEAN_SWEEP

    def test_a_dissolved_group_serves_a_memberless_detail_read(self, db):
        """`get_place_group_detail` short-circuits `place_balance` on an empty
        member list; the panel refetches immediately after a dissolve."""
        place = _Place(db, member_count=2)
        _seed(db, place.a, place.ua, "4")

        place.remove(place.a, reason="out")

        detail = CustomerLinkService().get_place_group_detail(place.group_id)
        assert detail is not None
        assert detail["members"] == []
        assert detail["place_balance"] == Decimal("0.00")
        assert len(detail["events"]) == 2
        assert AddressGroup.query.get(place.group_id) is not None

    # -- The design hole: a dissolved group is a reusable id -------------------- #

    @staticmethod
    def _try_to_repopulate_a_drifted_dissolved_group(db):
        """G dissolves with drift 20; a wholly unrelated funded address then tries
        to join the memberless group and is REFUSED.

        Returns (place, newcomer, newcomer_user)."""
        place = _Place(db, member_count=2)
        _seed(db, place.a, place.ua, "20")
        _drop_ledger_for_group(db, place.group_id)          # drift 20
        place.remove(place.a, reason="out")                 # dissolves onto b
        assert _group_ledger_sum(place.group_id) == Decimal("-20.00")

        newcomer_user = _user(db)
        newcomer = _addr(db, newcomer_user.id)
        _deliver(db, newcomer_user, newcomer, "6")
        _assert_refused(
            lambda: place.add([newcomer], reason="new tenant"), "PLACE_GROUP_DISSOLVED"
        )
        db.session.expire_all()
        return place, newcomer, newcomer_user

    def test_repopulating_a_dissolved_group_is_REFUSED_so_no_strangers_ledger_is_inherited(
        self, db
    ):
        """UPDATED: every number below changed. This used to pin the damage.

        The old pin: nothing stopped a memberless group id from being reused, and
        the departed members' entries are deliberately anchored to it for ever
        (`bottle_ledger.address_group_id` is a foreign key). So the new occupant's
        STORED balance was what they brought (6.00) while the place's ledger
        summed to their history MINUS the old drift — a phantom -20 they never
        created — and the destructive Reconcile button was ARMED against them:
        one press wrote -14.00 onto a customer who had six bottles.

        The refusal is evaluated under rung 0 (`address_groups(G)` FOR NO KEY
        UPDATE), so unlike an unlocked existence check it is not a TOCTOU. It
        prevents NEW exposure; it does not un-mix a group already re-populated
        before the fix, which needs a data audit. The structural answer is an
        incarnation/epoch column — a migration, flagged as an owner decision.
        """
        place, newcomer, _user_obj = self._try_to_repopulate_a_drifted_dissolved_group(db)

        assert UserAddress.query.get(newcomer.id).address_group_id is None
        assert _place(newcomer.id) == Decimal("6.00")
        # The stranger's residual is still anchored to the dead group, and stays
        # there — but no live address resolves to it any more.
        assert _group_ledger_sum(place.group_id) == Decimal("-20.00")
        # ...and the destructive button is no longer aimed at the newcomer.
        BottleTrackingService().reconcile_balance(newcomer.id)
        db.session.expire_all()
        assert _place(newcomer.id) == Decimal("6.00")

    def test_a_new_occupants_place_history_must_contain_only_their_own_places_entries(self, db):
        """FIXED — the xfail is gone.

        WAS: a memberless (previously dissolved) `AddressGroup` was accepted as a
        join target. `release_group_history_to_address` deliberately leaves the
        DEPARTED members' ledger entries stamped with the group for ever, so the
        new occupant's `get_place_ledger` — what the customer's own bottle history
        renders — listed entries belonging to strangers they had never shared a
        place with, and `build_merge_preview` offered those entries for exclusion.
        """
        place, newcomer, newcomer_user = self._try_to_repopulate_a_drifted_dissolved_group(db)

        history = BottleTrackingService.get_place_ledger(newcomer.id)
        strangers = [e for e in history["items"] if e.user_id != newcomer_user.id]
        assert strangers == [], (
            f"{len(strangers)} entries from users who were never this occupant's coworkers"
        )

    def test_a_ONE_member_place_is_no_longer_REACHABLE_through_any_service_path(
        self, db, client, app
    ):
        """UPDATED: this used to pin a reachable one-member place and its cost.

        The old pin: `create_place_group` refuses to build a one-member place,
        but neither `add_addresses_to_group` nor its route enforced any minimum,
        so re-populating a dissolved group with exactly one address produced one.
        The consequence reached the customer — that address became permanently
        undeletable from the Telegram bot and the app until an admin removed it,
        and every bottle read for it resolved through a group with no coworkers.

        Every door is now shut: >= 2 to create, a removal that would leave one
        member DISSOLVES in the same transaction, and a memberless group is
        refused as a join target. So the class is gone rather than merely
        mitigated — and the customer's own delete keeps working.
        """
        place = _Place(db, member_count=2)
        place.remove(place.a, reason="out")                    # memberless group
        tenant = _user(db)
        only_address = _addr(db, tenant.id)
        spare = _addr(db, tenant.id, "Home")                   # so "only address" never fires

        _assert_refused(
            lambda: place.add([only_address], reason="one tenant"), "PLACE_GROUP_DISSOLVED"
        )
        db.session.expire_all()

        assert UserAddress.query.filter_by(address_group_id=place.group_id).count() == 0
        detail = CustomerLinkService().get_place_group_detail(place.group_id)
        assert detail["members"] == []

        # The address stayed ungrouped, so the customer can still delete it.
        response = client.delete(
            f"/api/v1/addresses/{only_address.id}", headers=_headers(app, tenant)
        )
        assert response.status_code == 200, response.get_json()
        assert UserAddress.query.get(spare.id) is not None


# =========================================================================== #
# I. Moving an address between places
# =========================================================================== #


class TestMovingBetweenPlaces:
    def test_a_move_is_an_explicit_remove_then_add_and_conserves_both_places(self, db):
        source = _funded_place(db, "9", member_count=3)
        target = _funded_place(db, "4", member_count=2)
        total_before = _stored_total()
        assert total_before == Decimal("13.00")
        source_entry_ids = {
            e.id for e in BottleLedger.query.filter_by(address_group_id=source.group_id).all()
        }

        with pytest.raises(ValidationError) as exc:
            target.add([source.a])
        assert exc.value.error_code == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

        source.remove(source.a, reason="moving", bottles_leaving=3)
        target.add([source.a], reason="moved in")

        assert _place(source.b.id) == Decimal("6.00")
        assert _place(target.a.id) == Decimal("7.00")
        assert _place(source.a.id) == Decimal("7.00")       # same place as target now
        assert _own_row(source.a.id) is None
        assert _stored_total() == total_before              # the pair
        # The ':in' half moved with the address; the older source entries did not.
        in_half = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"place_leave:{source.group_id}:%:in")
        ).one()
        assert in_half.address_group_id == target.group_id
        for entry_id in source_entry_ids:
            assert BottleLedger.query.get(entry_id).address_group_id == source.group_id
        assert _sweep() == _CLEAN_SWEEP

    def test_moving_out_of_a_TWO_member_place_dissolves_the_source_first(self, db):
        """An admin performing a routine "move" silently dissolves the source
        place. The numbers must at least be right, and the survivor must be
        UNGROUPED before the join runs."""
        source = _funded_place(db, "10", member_count=2)
        target = _funded_place(db, "4", member_count=2)
        total_before = _stored_total()

        result = source.remove(source.a, reason="moving", bottles_leaving=4)

        assert result["dissolved"] is True
        db.session.refresh(source.b)
        assert source.b.address_group_id is None
        assert _place(source.b.id) == Decimal("6.00")
        assert _group_row(source.group_id) is None

        target.add([source.a], reason="moved in")

        assert _place(target.a.id) == Decimal("8.00")
        assert _stored_total() == total_before
        assert _sweep() == _CLEAN_SWEEP

    def test_a_move_round_trip_returns_both_places_to_their_original_state(self, db):
        """Four consecutive absorbs and splits, each re-stamping rows and
        rewriting snapshots. A single sign error compounds and only shows up as a
        non-zero delta at the end — so the pair is asserted at EVERY step."""
        source = _funded_place(db, "9", member_count=3)
        target = _funded_place(db, "4", member_count=2)
        total = _stored_total()

        def step(action):
            before = _stored_total()
            action()
            assert _stored_total() == before, "a step leaked bottles"

        step(lambda: source.remove(source.a, reason="out", bottles_leaving=2))
        assert (_place(source.b.id), _place(source.a.id)) == (Decimal("7.00"), Decimal("2.00"))
        step(lambda: target.add([source.a], reason="in"))
        assert _place(target.a.id) == Decimal("6.00")
        step(lambda: target.svc.remove_address_from_group(
            source.a.id, acting_admin_id=target.admin.id, reason="out again", bottles_leaving=2
        ))
        assert (_place(target.a.id), _place(source.a.id)) == (Decimal("4.00"), Decimal("2.00"))
        step(lambda: source.add([source.a], reason="back home"))

        assert _place(source.b.id) == Decimal("9.00")
        assert _place(target.a.id) == Decimal("4.00")
        assert _own_row(source.a.id) is None
        assert _stored_total() == total == Decimal("13.00")
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# I2. The customer's own two-tap "I moved" — the unfenced way INTO a place
# =========================================================================== #


class TestACustomerCanRePointAPlaceMemberAddress:
    """PINS THE ACTUAL OUTCOME (reported, not fixed). A fix must change this.

    Every OTHER route into and out of a place — create, join, leave, split,
    dissolve, delete — is admin-only, reason-mandatory and audited, precisely
    because it changes who can physically reach a shared pool.
    `PUT /api/v1/addresses/<id>` (api/addresses.py:145) changes exactly the same
    thing: it rewrites the coordinates and the street of a place-group MEMBER
    while leaving `address_group_id` untouched. It touches no place fence, takes
    no reason and writes no `CustomerLinkEvent`. It is customer-driven, it is two
    taps, and it is the only way a stranger's building can end up inside a place
    group without an admin ever acting.

    Asserted as one consequence set rather than six tests, because the point is
    that they all follow from the one unguarded write."""

    ELSEWHERE = (41.3300, 69.2000)          # ~7 km from the office, still in-zone

    @staticmethod
    def _office(db):
        admin = _admin(db)
        ua, ub = _user(db), _user(db)
        a1, a2 = _addr(db, ua.id, "Office"), _addr(db, ub.id, "Office")
        CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="same office 3F"
        )
        _deliver(db, ua, a1, "8")
        _deliver(db, ub, a2, "4")
        assert _place(a1.id) == Decimal("12.00")
        return admin, ua, ub, a1, a2

    def test_a_member_address_can_be_moved_ACROSS_TOWN_and_keeps_pooling_with_its_coworker(
        self, db, client, app
    ):
        admin, ua, ub, a1, a2 = self._office(db)
        group_id = a1.address_group_id
        events_before = CustomerLinkEvent.query.count()
        lat, lng = self.ELSEWHERE

        response = client.put(
            f"/api/v1/addresses/{a1.id}",
            json={
                "title": "Home",
                "full_address": "Chilanzar 12-14, Tashkent",
                "latitude": lat,
                "longitude": lng,
            },
            headers=_headers(app, ua),
        )

        # 1. It just works, and the address really moved.
        assert response.status_code == 200, response.get_json()
        db.session.expire_all()
        moved = UserAddress.query.get(a1.id)
        assert (float(moved.latitude), float(moved.longitude)) == (lat, lng)
        assert moved.title == "Home"
        assert moved.address_group_id == group_id          # still a place member

        # 2. A delivery to the customer's HOME now credits the OFFICE pool, and
        #    the coworker sees it on their own screen.
        _deliver(db, ua, a1, "3")
        assert _place(a2.id) == Decimal("15.00")
        coworker_view = client.get(
            "/api/v1/orders/bottles/my-balances", headers=_headers(app, ub)
        )
        assert coworker_view.status_code == 200, coworker_view.get_json()
        rows = coworker_view.get_json()["data"]["balances"]
        assert [r["place_balance"] for r in rows] == [15.0]
        assert rows[0]["place_group_id"] == group_id

        # 3. ...and a collection at the home draws the OFFICE pool down.
        driver = _user(db, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        BottleTrackingService().record_standalone_collection(
            ua.id, a1.id, Decimal("5"), actor_user_id=driver.id
        )
        db.session.commit()
        assert _place(a1.id) == _place(a2.id) == Decimal("10.00")

        # 4. The mover keeps reading their ex-coworker's history and name.
        assert CustomerLinkService().can_view_address_history(ua.id, a2.id) is True
        own_view = client.get(
            "/api/v1/orders/bottles/my-balances", headers=_headers(app, ua)
        )
        members = own_view.get_json()["data"]["balances"][0]["place_members"]
        assert sorted(m["member_name"] for m in members) == sorted(
            [f"{u.first_name} {u.last_name}" for u in (ua, ub)]
        )

        # 5. The admin panel shows one "place" spanning two buildings, with no
        #    marker of any kind — the member payload has nowhere to put one.
        detail = CustomerLinkService().get_place_group_detail(group_id)
        assert [m["address_id"] for m in detail["members"]] == sorted([a1.id, a2.id])
        assert sorted(detail["members"][0]) == [
            "address_id", "address_title", "full_address", "owner", "suggested_bottles_leaving"
        ]
        assert detail["place_balance"] == Decimal("10.00")

        # 6. Nothing anywhere recorded that a place member changed building.
        assert CustomerLinkEvent.query.count() == events_before
        assert [
            e for e in _events(group_id) if "Chilanzar" in (e.get("reason") or "")
        ] == []
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# J. The address-delete fence (the dimensions the sibling file does not cover)
# =========================================================================== #


@pytest.fixture
def fenced(db):
    """One customer whose SINGLE address is grouped AND used by a subscription."""
    owner = _user(db)
    coworker = _user(db)
    admin = _admin(db)
    address = _addr(db, owner.id)
    CustomerLinkService().create_place_group(
        [address.id, _addr(db, coworker.id).id], acting_admin_id=admin.id, reason="office"
    )
    subscription = Subscription(
        subscription_number=f"SUB-LIFE-{next(_SEQ)}",
        user_id=owner.id,
        status=SubscriptionStatus.ACTIVE,
        name="Weekly water",
        billing_cycle=SubscriptionFrequency.WEEKLY,
        billing_amount=Decimal("10.00"),
        next_billing_date=datetime.now(UTC) + timedelta(days=7),
        delivery_frequency=SubscriptionFrequency.WEEKLY,
        delivery_address_id=address.id,
        start_date=datetime.now(UTC),
        payment_method=PaymentMethod.CASH,
    )
    db.session.add(subscription)
    db.session.commit()
    db.session.refresh(address)
    assert address.address_group_id is not None
    return owner, address, admin


class TestTheDeleteFence:
    def test_the_place_fence_fires_AHEAD_of_the_only_address_and_subscription_checks(
        self, db, client, app, fenced
    ):
        """Each route hardcodes its own order, and two of three agreeing is not a
        fence. An admin told "this is the only address" would delete the other
        address first and still be blocked, with no idea why."""
        owner, address, admin = fenced

        customer = client.delete(
            f"/api/v1/addresses/{address.id}", headers=_headers(app, owner)
        )
        through_bot = client.delete(
            f"/api/v1/auth/addresses/{address.id}", headers=_headers(app, owner)
        )
        as_admin = client.delete(
            f"/api/v1/admin/users/{owner.id}/addresses/{address.id}",
            headers=_headers(app, admin, role=UserRole.ADMIN.value),
        )

        for response in (customer, through_bot, as_admin):
            body = response.get_json()
            assert response.status_code == 400, body
            codes = {body.get("error_code"), (body.get("data") or {}).get("error_code")}
            assert "PLACE_GROUP_ADDRESS_NOT_DELETABLE" in codes, body
            assert "only address" not in str(body).lower()
            assert "subscription" not in str(body).lower()
        assert UserAddress.query.get(address.id) is not None

    def test_each_route_carries_the_fence_in_a_DIFFERENT_body_shape(self, db, client, app,
                                                                    fenced):
        """THREE fences, three envelopes. `api/addresses.py` passes
        `errors={'address': msg}`, which the responder flattens to
        `"address: <msg>"`; `api/admin.py` passes a bare message; the auth route
        raises and `@handle_api_exception` puts the message at the TOP level with
        no `errors` list at all. Only `error_code` is common. Pinned so a future
        normalisation is a deliberate change and not a silent client break."""
        owner, address, admin = fenced
        english = (
            "Cannot delete an address that belongs to a place group — "
            "remove it from the place first"
        )

        customer = client.delete(
            f"/api/v1/addresses/{address.id}", headers=_headers(app, owner)
        ).get_json()
        through_bot = client.delete(
            f"/api/v1/auth/addresses/{address.id}", headers=_headers(app, owner)
        ).get_json()
        as_admin = client.delete(
            f"/api/v1/admin/users/{owner.id}/addresses/{address.id}",
            headers=_headers(app, admin, role=UserRole.ADMIN.value),
        ).get_json()

        # Customer route: FIELD-PREFIXED error list, generic top-level message.
        assert customer["message"] == "Validation failed"
        assert customer["errors"] == [f"address: {english}"]
        assert customer["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        # Admin route: the SAME text, with no field prefix.
        assert as_admin["message"] == "Validation failed"
        assert as_admin["errors"] == [english]
        assert as_admin["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        # Bot route: the text is the top-level message the bot renders verbatim,
        # and there is no `errors` list to parse at all.
        assert through_bot["message"] == english
        assert through_bot["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        assert "errors" not in through_bot
        # The one field all three share.
        assert (
            customer["data"]["error_code"]
            == as_admin["data"]["error_code"]
            == through_bot["error_code"]
        )

    def test_the_fence_message_is_translated_and_falls_back_to_ENGLISH_not_the_key(
        self, db, client, app, fenced
    ):
        """Translations are DB-backed, so an unseeded deploy is the normal state on
        day one. `get_translation` returns the KEY when unseeded, and the
        `if message == key` fallback is the only thing stopping a customer seeing
        `api.addresses.error.in_place_group` in Telegram."""
        owner, address, _admin_user = fenced
        key = "api.addresses.error.in_place_group"

        unseeded = client.delete(
            f"/api/v1/addresses/{address.id}?lang=ru", headers=_headers(app, owner)
        ).get_json()
        assert key not in str(unseeded)
        assert unseeded["errors"] == [
            "address: Cannot delete an address that belongs to a place group — "
            "remove it from the place first"
        ]
        assert unseeded["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"

        for language, value in (("ru", "Адрес входит в группу"), ("uz", "Manzil guruhda")):
            db.session.add(Translation(key=key, language=language, value=value, category="api"))
        db.session.commit()

        seeded = client.delete(
            f"/api/v1/addresses/{address.id}?lang=ru", headers=_headers(app, owner)
        ).get_json()
        assert seeded["errors"] == ["address: Адрес входит в группу"]
        assert seeded["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        # Both seeded languages are exercised: asserting only `ru` cannot tell a
        # working lookup from one that returns the FIRST row for the key.
        uzbek = client.delete(
            f"/api/v1/addresses/{address.id}?lang=uz", headers=_headers(app, owner)
        ).get_json()
        assert uzbek["errors"] == ["address: Manzil guruhda"]
        assert uzbek["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        assert UserAddress.query.get(address.id) is not None

    def test_a_cross_user_delete_probe_404s_without_leaking_the_fence(self, db, client, app,
                                                                     fenced):
        """The customer route filters by (id, user_id) BEFORE the fence, so a
        stranger probing which addresses are place-grouped must get a plain 404."""
        _owner, address, _admin_user = fenced
        stranger = _user(db)
        _addr(db, stranger.id)

        response = client.delete(
            f"/api/v1/addresses/{address.id}", headers=_headers(app, stranger)
        )

        assert response.status_code == 404
        assert "PLACE_GROUP_ADDRESS_NOT_DELETABLE" not in str(response.get_json())
        assert UserAddress.query.get(address.id) is not None

    def test_an_ungrouped_address_still_deletes_through_all_three_paths(self, db, client, app):
        """The fence adds a SELECT to every delete in the product; a mistake in
        `row is not None and row[0] is not None` would block them all."""
        owner = _user(db)
        admin = _admin(db)
        first, second, third, spare = (_addr(db, owner.id) for _ in range(4))

        customer = client.delete(
            f"/api/v1/addresses/{first.id}", headers=_headers(app, owner)
        )
        as_admin = client.delete(
            f"/api/v1/admin/users/{owner.id}/addresses/{second.id}",
            headers=_headers(app, admin, role=UserRole.ADMIN.value),
        )
        AuthService().delete_user_address(owner.id, third.id)

        assert customer.status_code == 200, customer.get_json()
        assert as_admin.status_code == 200, as_admin.get_json()
        assert UserAddress.query.filter_by(user_id=owner.id).count() == 1
        assert UserAddress.query.get(spare.id) is not None

    def test_the_lifecycle_routes_enforce_their_own_permission_level(self, db, client, app):
        """The three MUTATING place-group routes need `manage_users` (ADMIN only,
        since MANAGER's permission list does not carry it); the detail READ needs
        `view_users` (ADMIN or MANAGER); a customer is refused everywhere."""
        place = _funded_place(db, "5", member_count=3)
        joiner = _addr(db, _user(db).id)
        manager = _user(db, role=UserRole.MANAGER, user_type=UserType.STAFF)
        operator = _user(db, role=UserRole.OPERATOR, user_type=UserType.STAFF)
        customer = _user(db)
        before = _snapshot()

        def mutations(identity, role=None):
            headers = _headers(app, identity, role=role)
            return [
                client.post(
                    "/api/v1/admin/place-groups",
                    json={"addressIds": [joiner.id, place.a.id], "reason": "x"},
                    headers=headers,
                ),
                client.post(
                    f"/api/v1/admin/place-groups/{place.group_id}/addresses",
                    json={"addressIds": [joiner.id], "reason": "x"},
                    headers=headers,
                ),
                client.delete(
                    f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
                    json={"reason": "x"},
                    headers=headers,
                ),
            ]

        for identity in (manager, operator, customer):
            for response in mutations(identity):
                assert response.status_code == 403, (identity.role, response.get_json())
        assert _snapshot() == before

        # Reads: manager may, operator and customer may not.
        detail_url = f"/api/v1/admin/place-groups/{place.group_id}"
        assert client.get(detail_url, headers=_headers(app, manager)).status_code == 200
        assert client.get(detail_url, headers=_headers(app, operator)).status_code == 403
        assert client.get(detail_url, headers=_headers(app, customer)).status_code == 403
        # And the admin address delete needs manager-or-higher (JWT CLAIMS role).
        grouped = place.a
        assert client.delete(
            f"/api/v1/admin/users/{place.ua.id}/addresses/{grouped.id}",
            headers=_headers(app, operator, role=UserRole.OPERATOR.value),
        ).status_code == 403
        assert _snapshot() == before

    def test_the_removal_route_returns_JSON_primitives_the_panel_can_do_maths_on(
        self, db, client, admin_auth_headers
    ):
        """Flask's provider renders a bare Decimal as the STRING "4.00", and
        `dissolved` is the panel's only signal that the group it was editing no
        longer has members."""
        place = _funded_place(db, "10", member_count=2)

        response = client.delete(
            f"/api/v1/admin/place-groups/{place.group_id}/addresses/{place.a.id}",
            json={"reason": "left", "bottlesLeaving": 4},
            headers=admin_auth_headers,
        )

        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["place_group_id"] == place.group_id
        assert data["bottles_leaving"] == 4.0 and isinstance(data["bottles_leaving"], float)
        assert data["dissolved"] is True
        assert sorted(data) == ["bottles_leaving", "dissolved", "place_group_id"]


# =========================================================================== #
# K. What a membership change does to everything else
# =========================================================================== #


class TestLifecycleSideEffects:
    def test_a_delivery_replay_after_a_join_stays_idempotent(self, db):
        """`delivery:{order}` is matched GLOBALLY by key, which is scope-blind —
        and here that is CORRECT. Pinned so a "fix" to the idempotency check
        (see the set_initial_balance bug below) cannot re-introduce
        double-counted deliveries."""
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        order = _order(db, u1, a1)
        service = BottleTrackingService()
        first = service.record_bottles_delivered(order.id, u1.id, a1.id, Decimal("3"))
        db.session.commit()
        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )
        rows_before, total_before = BottleLedger.query.count(), _stored_total()

        replay = service.record_bottles_delivered(order.id, u1.id, a1.id, Decimal("3"))
        db.session.commit()

        assert replay.id == first.id
        assert replay.address_group_id == group.id
        assert BottleLedger.query.count() == rows_before
        assert _stored_total() == total_before == Decimal("3.00")
        assert _place(a1.id) == Decimal("3.00")

    def test_set_initial_balance_after_a_join_then_leave_RECORDS(self, db):
        """UPDATED: every number below changed when the idempotency key was dropped.

        This used to pin the damage. `initial:addr:<id>` survived the re-stamp
        onto the group, `_create_ledger_entry`'s duplicate lookup is
        `filter_by(idempotency_key=...)` with NO scope predicate while
        `set_initial_balance`'s has-history guard IS scope-filtered — so the
        admin re-opening a departed address got a 200 carrying somebody else's
        place-scoped entry and zero bottles were recorded.

        The key is gone. Adding a scope predicate to that lookup was not the
        alternative: `uq_bottle_ledger_idempotency` is UNIQUE on the key alone,
        so it would have turned the silent no-op into an IntegrityError 500. The
        real guard is structural and unaffected.
        """
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        service = BottleTrackingService()
        original = service.set_initial_balance(u1.id, a1.id, Decimal("5"), actor_user_id=admin.id)
        db.session.commit()
        assert original.idempotency_key is None
        CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )
        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        db.session.expire_all()
        assert _place(a1.id) == Decimal("0.00")
        assert _own_ledger_sum(a1.id) == Decimal("0.00")
        rows_before = BottleLedger.query.count()

        returned = service.set_initial_balance(
            None, a1.id, Decimal("7"), actor_user_id=admin.id, notes="reopening"
        )
        db.session.commit()

        assert returned.id != original.id                   # a NEW entry...
        assert returned.quantity == Decimal("7.00")
        assert returned.address_group_id is None            # ...in A1's OWN scope
        assert returned.address_id == a1.id
        assert BottleLedger.query.count() == rows_before + 1
        assert _place(a1.id) == Decimal("7.00")

    def test_set_initial_balance_after_a_join_then_leave_must_record_or_refuse(self, db):
        admin = _admin(db)
        u1, u2 = _user(db), _user(db)
        a1, a2 = _addr(db, u1.id), _addr(db, u2.id)
        service = BottleTrackingService()
        service.set_initial_balance(u1.id, a1.id, Decimal("5"), actor_user_id=admin.id)
        db.session.commit()
        CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office"
        )
        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        db.session.expire_all()

        try:
            service.set_initial_balance(
                None, a1.id, Decimal("7"), actor_user_id=admin.id, notes="reopening"
            )
            db.session.commit()
        except ValidationError:
            return          # a NAMED refusal is an acceptable fix, and xpasses here

        assert _place(a1.id) == Decimal("7.00")

    @staticmethod
    def _dissolved_group_and_two_new_tenants(db, client, app):
        """UPDATED: the trap this fixture used to BUILD is now unreachable.

        It composed two features nobody owned together. `set_initial_balance`
        seeds the PLACE, so the key was `initial:place:{G}`; removing the
        coworker left the seeded member as the last one out, so §7.3 re-stamped
        that member's OWN entries — INITIAL_BALANCE row included, key intact —
        into its ADDRESS scope and DELETEd G's balance row; and
        `add_addresses_to_group` then accepted the memberless G as a join target,
        making G live again with two brand-new occupants who inherited a
        stranger's residual and burned their own one-shot seed.

        BOTH links in that chain are now cut, and the fixture asserts each: the
        INITIAL_BALANCE row carries no key at all, and the route REFUSES the
        dissolved group by name.

        Returns the facts the two tests below assert on."""
        admin = _admin(db)
        ua, ub = _user(db), _user(db)
        a, b = _addr(db, ua.id), _addr(db, ub.id)
        svc, service = CustomerLinkService(), BottleTrackingService()
        group = svc.create_place_group([a.id, b.id], acting_admin_id=admin.id, reason="office")
        original = service.set_initial_balance(ua.id, a.id, Decimal("5"), actor_user_id=admin.id)
        db.session.commit()
        assert original.idempotency_key is None

        svc.remove_address_from_group(b.id, acting_admin_id=admin.id, reason="left")
        db.session.expire_all()
        assert _place(a.id) == Decimal("5.00")
        assert BottleLedger.query.get(original.id).address_group_id is None   # now A's own scope
        assert _group_row(group.id) is None
        assert _group_ledger_sum(group.id) == Decimal("0.00")

        uc, ud = _user(db), _user(db)
        c, d = _addr(db, uc.id), _addr(db, ud.id)
        response = client.post(
            f"/api/v1/admin/place-groups/{group.id}/addresses",
            json={"addressIds": [c.id, d.id], "reason": "new tenants moved in"},
            headers=_headers(app, admin),
        )
        return {
            "admin": admin, "group_id": group.id, "seeded_address_id": a.id,
            "seeded_owner_id": ua.id, "original_id": original.id,
            "new_address_id": c.id, "new_owner_id": uc.id, "other_new_address_id": d.id,
            "repopulate_response": response,
        }

    def test_a_DISSOLVED_group_is_REFUSED_as_a_join_target_by_the_live_route(
        self, db, client, app
    ):
        """UPDATED: every figure below changed. This used to pin the silent no-op.

        The old pin: the route returned 200, G was live again with two real
        occupants, and `set_initial_balance` for them returned 200 echoing a
        DIFFERENT customer's ledger row in a DIFFERENT scope while recording
        nothing — consuming the admin's one-shot chance to seed the new tenants'
        opening count. Conservation could not see it, the nightly sweep could not
        see it, and both figures still agreed at every place.

        A group id must denote exactly ONE tenancy. §7.1/§7.3 deliberately leave
        departed members' ledger rows stamped with the group they left, and
        `bottle_ledger.address_group_id` is a foreign key, so a dissolved group
        keeps its history for ever — re-populating the id hands the new members a
        stranger's delivery history through `get_place_ledger`, which filters on
        `address_group_id` alone. The refusal is evaluated while holding
        `address_groups(G)` (rung 0), so it is not a TOCTOU.
        """
        facts = self._dissolved_group_and_two_new_tenants(db, client, app)
        response = facts["repopulate_response"]

        assert response.status_code == 400, response.get_json()
        payload = response.get_json()
        code = (payload.get("data") or {}).get("error_code") or payload.get("error_code")
        assert code == "PLACE_GROUP_DISSOLVED", payload
        # Nothing moved: the new tenants are still ungrouped and unfunded, and
        # the departed member keeps the history that was released to it.
        db.session.expire_all()
        assert UserAddress.query.get(facts["new_address_id"]).address_group_id is None
        assert _place(facts["new_address_id"]) == Decimal("0.00")
        assert _place(facts["seeded_address_id"]) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_the_new_tenants_get_their_own_place_and_their_own_one_shot_seed(
        self, db, client, app
    ):
        """The other half of the refusal: it must not cost the admin anything.

        Replaces the strict xfail `..._must_record_or_refuse`, which demanded
        that seeding a re-populated dissolved group either record or be named.
        It is named — and this pins the recovery path the admin actually has:
        create a FRESH place for the new tenants and seed it, which records the
        full twelve bottles because no stale key can swallow them any more.
        """
        facts = self._dissolved_group_and_two_new_tenants(db, client, app)
        assert facts["repopulate_response"].status_code == 400

        fresh = client.post(
            "/api/v1/admin/place-groups",
            json={
                "addressIds": [facts["new_address_id"], facts["other_new_address_id"]],
                "reason": "new tenants moved in",
            },
            headers=_headers(app, facts["admin"]),
        )
        assert fresh.status_code in (200, 201), fresh.get_json()

        response = client.post(
            "/api/v1/admin/bottles/initial-balance",
            json={"address_id": facts["new_address_id"], "quantity": 12, "notes": "opening count"},
            headers=_headers(app, facts["admin"]),
        )
        assert response.status_code == 200, response.get_json()
        echoed = response.get_json()["data"]
        assert echoed["address_id"] == facts["new_address_id"]
        assert echoed["quantity"] == 12.0
        assert _place(facts["new_address_id"]) == Decimal("12.00")
        assert _place(facts["seeded_address_id"]) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_place_write_re_attributes_when_the_REPRESENTATIVE_member_leaves(self, db):
        """`resolve_place_attribution_user_id` is the SSOT the admin UI's
        `representative_address_id` mirrors: the lowest member address id. A
        membership change silently changes who a place-level write is booked
        against, and that has to be the same rule on both sides."""
        place = _funded_place(db, "0", member_count=3)
        service = BottleTrackingService()
        assert place.a.id == min(a.id for a in place.addrs)

        first = service.admin_adjust_balance(
            None, place.b.id, Decimal("2"), actor_user_id=place.admin.id, notes="count"
        )
        db.session.commit()
        assert first.user_id == place.ua.id                  # the lowest-id member's owner

        place.remove(place.a, reason="left")                 # three members -> two, no dissolve
        db.session.expire_all()
        assert _place(place.a.id) == Decimal("0.00")         # left with nothing, by default
        total_before = _stored_total()

        second = service.admin_adjust_balance(
            None, place.b.id, Decimal("1"), actor_user_id=place.admin.id, notes="count again"
        )
        db.session.commit()
        assert second.user_id == place.ub.id                 # representative moved along
        assert second.address_group_id == place.group_id     # ...and it landed on the PLACE
        assert _place(place.b.id) == _place(place.c.id) == Decimal("3.00")
        assert _place(place.a.id) == Decimal("0.00")         # not on the departed address
        assert _stored_total() == total_before + Decimal("1.00")

    def test_a_coworkers_standalone_collection_is_blocked_once_they_leave(self, db):
        """`_assert_user_in_scope` reads LIVE membership — the group lifecycle is
        the only thing that flips its answer, and a driver mid-route is holding a
        stale customer list."""
        place = _funded_place(db, "6", member_count=3)
        driver = _user(db, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        service = BottleTrackingService()

        # Legal today: ub owns a member address of the same place.
        entry = service.record_standalone_collection(
            place.ub.id, place.a.id, Decimal("2"), actor_user_id=driver.id
        )
        db.session.commit()
        assert entry.quantity == Decimal("-2.00")
        assert _place(place.a.id) == Decimal("4.00")

        place.remove(place.b, reason="ub moved out")
        db.session.expire_all()
        assert (_place(place.a.id), _place(place.b.id)) == (Decimal("4.00"), Decimal("0.00"))
        total_before = _stored_total()
        before = _snapshot()

        with pytest.raises(ValidationError) as exc:
            service.record_standalone_collection(
                place.ub.id, place.a.id, Decimal("2"), actor_user_id=driver.id
            )

        assert exc.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
        assert _stored_total() == total_before
        # A refusal writes NOTHING — neither at the place it was aimed at nor at
        # the departed address the collector now belongs to.
        assert _snapshot() == before
        assert (_place(place.a.id), _place(place.b.id)) == (Decimal("4.00"), Decimal("0.00"))

    def test_a_delivery_after_a_removal_books_to_the_DEPARTED_addresss_own_scope(self, db):
        """`record_bottles_delivered` resolves the scope at WRITE time from the
        live pointer, so the same dispatched order books to a different place
        depending on when the removal happened. Confirmed as intended and pinned,
        because the driver's app showed the PLACE's number at dispatch."""
        place = _funded_place(db, "5", member_count=3)
        order = _order(db, place.ua, place.a)
        total_before = _stored_total()

        place.remove(place.a, reason="left before the van arrived")
        BottleTrackingService().record_bottles_delivered(
            order.id, place.ua.id, place.a.id, Decimal("3")
        )
        db.session.commit()

        assert _place(place.b.id) == Decimal("5.00")            # the place is untouched
        assert _place(place.a.id) == Decimal("3.00")            # ...it landed on its own scope
        entry = BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").one()
        assert entry.address_group_id is None
        assert _stored_total() == total_before + Decimal("3.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_the_nightly_sweep_is_clean_after_EVERY_transition(self, db):
        """`stranded_address_balances` is the exact inverse of
        `orphaned_place_balances` and neither can see the other's shape — a
        transient address-keyed row left by a split (the ':in' half runs while the
        address is still grouped) is caught only here."""
        admin = _admin(db)
        users = [_user(db) for _ in range(6)]
        addrs = [_addr(db, u.id) for u in users]
        for index in (0, 1, 2, 5):
            _deliver(db, users[index], addrs[index], "4")
        svc = CustomerLinkService()
        steps = []

        def step(name, action):
            action()
            db.session.expire_all()
            steps.append((name, _sweep()))

        funded = {}
        step("create_funded", lambda: funded.update(
            group=svc.create_place_group(
                [addrs[0].id, addrs[1].id, addrs[2].id],
                acting_admin_id=admin.id, reason="create funded",
            )
        ))
        historyless = {}
        step("create_historyless", lambda: historyless.update(
            group=svc.create_place_group(
                [addrs[3].id, addrs[4].id], acting_admin_id=admin.id, reason="create historyless"
            )
        ))
        group_id, other_id = funded["group"].id, historyless["group"].id
        step("add_funded_joiner", lambda: svc.add_addresses_to_group(
            group_id, [addrs[5].id], acting_admin_id=admin.id, reason="add funded"))
        step("remove_default", lambda: svc.remove_address_from_group(
            addrs[5].id, acting_admin_id=admin.id, reason="default out"))
        step("add_to_a_place_with_no_balance_row", lambda: svc.add_addresses_to_group(
            other_id, [addrs[5].id], acting_admin_id=admin.id, reason="late create"))
        step("remove_with_split", lambda: svc.remove_address_from_group(
            addrs[0].id, acting_admin_id=admin.id, reason="split out", bottles_leaving=2))
        step("dissolve_to_one_member", lambda: svc.remove_address_from_group(
            addrs[1].id, acting_admin_id=admin.id, reason="dissolve onto the survivor"))
        step("remove_from_a_three_member_place", lambda: svc.remove_address_from_group(
            addrs[3].id, acting_admin_id=admin.id, reason="out"))
        step("dissolve_the_second_place", lambda: svc.remove_address_from_group(
            addrs[4].id, acting_admin_id=admin.id, reason="dissolve"))
        # UPDATED: `repopulate_a_memberless_place` used to be step 10 here. A
        # dissolved group is now REFUSED as a join target (PLACE_GROUP_DISSOLVED),
        # so that transition no longer exists — the refusal is asserted as the
        # transition, and the sweep must be clean after it too.
        step("repopulating_a_memberless_place_is_REFUSED", lambda: _assert_refused(
            lambda: svc.add_addresses_to_group(
                other_id, [addrs[5].id], acting_admin_id=admin.id, reason="one tenant"
            ),
            "PLACE_GROUP_DISSOLVED",
        ))
        # ...and the ZERO-REMAINING dissolve arm still has to work for the
        # one-member places that exist in data written before that refusal, so
        # the state is built by hand (see `_force_membership`).
        step("dissolve_to_zero_members_from_a_HAND_BUILT_one_member_place", lambda: (
            _force_membership(db, addrs[5].id, other_id),
            svc.remove_address_from_group(
                addrs[5].id, acting_admin_id=admin.id, reason="closed"
            ),
        ))

        assert len(steps) == 11
        assert [name for name, report in steps if report != _CLEAN_SWEEP] == []


# =========================================================================== #
# L. The whole-lifecycle conservation property
# =========================================================================== #


def test_the_whole_lifecycle_conserves_at_EVERY_step(db):
    """The only test that catches a leak hiding inside a single step's own
    arithmetic — e.g. a dissolve that credits the survivor from `place_total`
    while a split already moved part of it. `Σ bottle_balances` is asserted as a
    PAIR around every transition, and the deliveries/returns are the only two
    steps allowed to change it (by exactly their own quantity)."""
    admin = _admin(db)
    svc = CustomerLinkService()
    users = [_user(db) for _ in range(3)]
    a1, a2, a3 = (_addr(db, u.id) for u in users)
    _deliver(db, users[0], a1, "6")
    _deliver(db, users[1], a2, "5")
    _deliver(db, users[2], a3, "4")
    expected = Decimal("15.00")
    assert _stored_total() == expected

    def step(action, *, delta=Decimal("0.00")):
        nonlocal expected
        before = _stored_total()
        action()
        db.session.expire_all()
        expected = before + delta
        assert _stored_total() == expected, "the step did not conserve"

    step(lambda: svc.create_place_group(
        [a1.id, a2.id], acting_admin_id=admin.id, reason="create"))
    group_id = UserAddress.query.get(a1.id).address_group_id
    assert _place(a1.id) == Decimal("11.00")

    step(lambda: svc.add_addresses_to_group(
        group_id, [a3.id], acting_admin_id=admin.id, reason="add"))
    assert _place(a1.id) == Decimal("15.00")

    step(lambda: _deliver(db, users[1], a2, "3"), delta=Decimal("3.00"))
    assert _place(a1.id) == Decimal("18.00")

    step(lambda: _give_back(db, users[2], a3, "2"), delta=Decimal("-2.00"))
    assert _place(a1.id) == Decimal("16.00")

    step(lambda: svc.remove_address_from_group(
        a1.id, acting_admin_id=admin.id, reason="out with four", bottles_leaving=4))
    assert (_place(a2.id), _place(a1.id)) == (Decimal("12.00"), Decimal("4.00"))

    step(lambda: svc.add_addresses_to_group(
        group_id, [a1.id], acting_admin_id=admin.id, reason="back"))
    assert _place(a1.id) == Decimal("16.00")

    step(lambda: svc.remove_address_from_group(a3.id, acting_admin_id=admin.id, reason="a3 out"))
    assert _place(a1.id) == Decimal("16.00")
    assert _place(a3.id) == Decimal("0.00")

    step(lambda: svc.remove_address_from_group(a2.id, acting_admin_id=admin.id, reason="a2 out"))

    # One address-keyed row holds everything; no group row survives.
    assert _place(a1.id) == Decimal("16.00")
    assert _stored_total() == Decimal("16.00")
    rows = BottleBalance.query.all()
    assert len(rows) == 1
    assert (rows[0].address_id, rows[0].address_group_id) == (a1.id, None)
    assert AddressGroup.query.get(group_id) is not None
    assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# L2. Correcting a DELIVERED order after its address left the place
# =========================================================================== #


class TestCorrectingAnOrderAfterItsAddressLeftThePlace:
    """Two writers correcting a historical event disagree about which scope
    history belongs to, and the codebase never states the inconsistency.

    `bottle_fines` deliberately FREEZE their scope at issue time
    (`BottleTrackingService._fine_scope`, and models/bottle.py documents why:
    a later ungrouping must not split the FINE_ISSUED / FINE_PAID pair across
    two ledgers). `OrderEditService._cascade_bottle` (order_edit_service.py:918)
    deliberately RE-RESOLVES it: it calls `_create_ledger_entry` with
    `address_id=order.delivery_address_id` and NO explicit scope, so
    `resolve_scope` reads the address's membership as of TODAY.

    A coworker leaving is routine (§5.1 calls re-scoping routine) and correcting
    their last delivered order days later is the commonest admin bottle action,
    so the two meet constantly. What the tests below pin is that the correction
    lands somewhere the delivery never was — and that the two figures the rest of
    the suite guards (global conservation and per-place stored == ledger_sum)
    are BOTH satisfied while it happens. That is the BUG 8 shape exactly."""

    @staticmethod
    def _delivered_at_the_place_then_left(db, *, member_count):
        """Six bottles delivered to A while A is grouped, then A leaves with the
        DEFAULT `bottles_leaving` (0). `remove_address_from_group` never
        re-stamps, so A's `delivery:{order}` row keeps `address_group_id = G`
        (`absorb_address_into_group`'s docstring states this explicitly).

        member_count=3 -> the place survives and keeps the bottles.
        member_count=2 -> §7.3 dissolves it onto a DIFFERENT survivor and the
        value crosses via `inherited`, while the delivery row stays behind in the
        now-memberless group."""
        from business_app.services.order_service import OrderService

        admin = _admin(db)
        users = [_user(db) for _ in range(member_count)]
        addrs = [_addr(db, u.id) for u in users]
        group = CustomerLinkService().create_place_group(
            [a.id for a in addrs], acting_admin_id=admin.id, reason="office"
        )
        product = _bottle_product(db, per_unit="1")
        order = _order_with_item(
            db, users[0], product, addrs[0], quantity=6, status=OrderStatus.OUT_FOR_DELIVERY
        )
        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)
        db.session.commit()
        db.session.expire_all()
        assert _place(addrs[0].id) == Decimal("6.00")
        delivery_row = BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").one()
        assert delivery_row.address_group_id == group.id

        result = CustomerLinkService().remove_address_from_group(
            addrs[0].id, acting_admin_id=admin.id, reason="moved out"
        )
        db.session.expire_all()
        assert result["dissolved"] is (member_count == 2)
        assert _place(addrs[0].id) == Decimal("0.00")
        return {
            "admin": admin, "order": order, "product": product, "group_id": group.id,
            "departed": addrs[0], "stayed": addrs[1], "dissolved": result["dissolved"],
        }

    @staticmethod
    def _edit_down_by_five(facts):
        from business_app.services.order_edit_service import OrderEditItemSpec, OrderEditService

        order = Order.query.get(facts["order"].id)
        return OrderEditService().apply_edit(
            order_id=order.id,
            items=[
                OrderEditItemSpec(
                    product_id=facts["product"].id,
                    quantity=1,
                    order_item_id=order.order_items[0].id,
                )
            ],
            reason="the driver miscounted; five of the six were never left",
            actor_user_id=facts["admin"].id,
        )

    def test_the_correction_lands_in_the_PLACE_that_holds_the_crates(self, db):
        """UPDATED: every number below changed when `_cascade_bottle` froze its scope.

        This used to pin the damage: the customer's own screen read -5
        (over-returned) for an address that had never returned anything, and the
        office was silently five crates over on its statement. Both oracles the
        rest of the suite relies on were satisfied by that state — global
        conservation netted to zero and BOTH scopes satisfied
        `get_place_balance == ledger_sum` — which is why it needed a per-scope
        attribution pin then, and still does now.
        """
        facts = self._delivered_at_the_place_then_left(db, member_count=3)
        departed, stayed = facts["departed"], facts["stayed"]
        total_before = _stored_total()

        self._edit_down_by_five(facts)
        db.session.expire_all()

        # The correction went to the scope that holds the crates.
        assert _place(departed.id) == Decimal("0.00")
        assert _own_row(departed.id) is None, (
            "the departed address gained a scope of its own out of an order edit"
        )
        assert _place(stayed.id) == Decimal("1.00")
        assert _group_ledger_sum(facts["group_id"]) == Decimal("1.00")
        correction = (
            BottleLedger.query.filter_by(order_id=facts["order"].id)
            .filter(BottleLedger.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT)
            .one()
        )
        assert correction.quantity == Decimal("-5.00")
        assert correction.address_id == departed.id, (
            "the ATTRIBUTION stamp still names the door the order went through — "
            "only the SCOPE is frozen"
        )
        assert correction.address_group_id == facts["group_id"]
        assert BottleLedger.query.filter_by(
            idempotency_key=f"delivery:{facts['order'].id}"
        ).one().address_group_id == facts["group_id"]

        assert _stored_total() == total_before - Decimal("5.00")
        assert _group_row(facts["group_id"]).balance == _group_ledger_sum(facts["group_id"])
        # The alarm that used to fire on the innocent customer is silent.
        assert _sweep() == _CLEAN_SWEEP

    def test_the_correction_FOLLOWS_the_dissolve_onto_the_place_that_HOLDS_the_crates(
        self, db
    ):
        """UPDATED AGAIN: the named refusal became a FORWARDED write, and lands right.

        The history of this one arm is the whole point of the forwarding pointer.
        FIRST it was silent corruption: the removal DISSOLVED the place, the six
        bottles crossed to the survivor's own scope via `inherited` while the
        `delivery:{order}` row stayed behind in the memberless group, and the
        correction reached NEITHER — it booked -5 onto the departed address,
        minting a negative scope for someone who left with nothing.

        THEN it was a named refusal (`BOTTLE_CORRECTION_SCOPE_NOT_LIVE`), because
        the frozen scope is a group whose `bottle_balances` row was DELETED and
        booking there would re-mint precisely the orphan §7.3 exists to
        eliminate. Honest, and a dead end for the admin.

        NOW `address_groups.dissolved_onto_address_id` records which address the
        dissolve released the place's history onto, so the correction follows the
        history to the scope that actually holds the crates. Every figure below
        is the INTENDED one the first two versions of this test could not reach.
        """
        facts = self._delivered_at_the_place_then_left(db, member_count=2)
        departed, survivor = facts["departed"], facts["stayed"]
        assert facts["dissolved"] is True
        assert _place(survivor.id) == Decimal("6.00")
        total_before = _stored_total()

        # The dissolve left the pointer, and it names the SURVIVOR address.
        group = AddressGroup.query.get(facts["group_id"])
        assert group.dissolved_onto_address_id == survivor.id

        self._edit_down_by_five(facts)
        db.session.commit()
        db.session.expire_all()

        # THE CRATES. Five of the six came off the place that actually holds
        # them, and the departed address gained no scope at all.
        assert _place(survivor.id) == Decimal("1.00")
        assert _place(departed.id) == Decimal("0.00")
        assert _own_row(departed.id) is None, (
            "the departed address gained a scope of its own out of a forwarded "
            "correction — the exact orphan the forwarding pointer exists to avoid"
        )

        # THE ATTRIBUTION. A forwarded entry MUST be attributed to the survivor:
        # an address scope's ledger predicate is `address_id = X AND
        # address_group_id IS NULL`, so attributing it to the departed address
        # would put the entry and the balance it moved in two different scopes.
        correction = (
            BottleLedger.query.filter_by(order_id=facts["order"].id)
            .filter(BottleLedger.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT)
            .one()
        )
        assert correction.quantity == Decimal("-5.00")
        assert correction.address_id == survivor.id
        assert correction.address_group_id is None
        # ...and the door the episode actually came through survives in the
        # metadata, which is the only place it still exists.
        assert correction.entry_metadata["forwarded_from_place_group_id"] == facts["group_id"]
        assert correction.entry_metadata["forwarded_to_address_id"] == survivor.id
        assert correction.entry_metadata["attributed_through_address_id"] == departed.id

        # NO ORPHAN. The dead group's balance row stays deleted — the forwarded
        # write never re-created it — and the delivery row it corrects is still
        # frozen where it always was.
        assert _group_row(facts["group_id"]) is None
        assert BottleLedger.query.filter_by(
            idempotency_key=f"delivery:{facts['order'].id}"
        ).one().address_group_id == facts["group_id"]

        assert _stored_total() == total_before - Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_the_correction_is_still_REFUSED_when_the_dissolve_left_NO_pointer(self, db):
        """The refusal STAYS for the case that genuinely has no destination.

        Two ways to reach it: a place that dissolved BEFORE the pointer column
        existed and whose audit row the migration's backfill could not resolve,
        and one whose survivor address has since been deleted (the FK is ON
        DELETE SET NULL). NULLing the pointer here reproduces both.

        Inventing a scope for these would be the silent corruption the refusal
        replaced, so `BOTTLE_CORRECTION_SCOPE_NOT_LIVE` must survive the fix —
        the pointer widens the green path, it does not remove the guard behind
        it.
        """
        facts = self._delivered_at_the_place_then_left(db, member_count=2)
        departed, survivor = facts["departed"], facts["stayed"]
        assert facts["dissolved"] is True

        AddressGroup.query.filter_by(id=facts["group_id"]).update(
            {AddressGroup.dissolved_onto_address_id: None}, synchronize_session=False
        )
        db.session.commit()
        db.session.expire_all()
        total_before = _stored_total()

        with pytest.raises(ValidationError) as exc:
            self._edit_down_by_five(facts)
        assert exc.value.error_code == "BOTTLE_CORRECTION_SCOPE_NOT_LIVE"
        db.session.rollback()
        db.session.expire_all()

        # NOTHING was written: no phantom scope for the departed address, the
        # survivor untouched, and no orphan row re-minted for the dead group.
        assert _place(departed.id) == Decimal("0.00")
        assert _own_row(departed.id) is None
        assert _place(survivor.id) == Decimal("6.00")
        assert _group_row(facts["group_id"]) is None
        assert BottleLedger.query.filter_by(
            idempotency_key=f"delivery:{facts['order'].id}"
        ).one().address_group_id == facts["group_id"]
        assert _stored_total() == total_before
        assert _sweep() == _CLEAN_SWEEP

    def test_the_correction_must_land_in_the_same_scope_as_the_delivery_it_corrects(self, db):
        """FIXED — the xfail is gone.

        WAS: `_cascade_bottle` resolved the bottle scope LIVE from
        `order.delivery_address_id`, so a correction to an ALREADY-DELIVERED
        order booked to wherever that address happened to be TODAY rather than to
        the scope the `delivery:{order_id}` row it corrects is stamped to. After
        the address left its place (routine per §5.1, and
        `remove_address_from_group` deliberately never re-stamps), the -N
        ADMIN_ADJUSTMENT created the departed address's brand-new own scope AT A
        NEGATIVE while the place kept the full delivered quantity. Invisible to
        every oracle in the suite: global conservation netted to zero and BOTH
        scopes still satisfied `get_place_balance == ledger_sum`.

        It was also an INCONSISTENCY the codebase never stated: `bottle_fines`
        FREEZE their scope at issue time while this cascade re-resolved it — two
        writers correcting a historical event disagreeing about which scope
        history belongs to. Both now FREEZE, and the lifecycle carries both
        frozen references.
        """
        facts = self._delivered_at_the_place_then_left(db, member_count=3)

        self._edit_down_by_five(facts)
        db.session.expire_all()

        delivery = BottleLedger.query.filter_by(
            idempotency_key=f"delivery:{facts['order'].id}"
        ).one()
        correction = (
            BottleLedger.query.filter_by(order_id=facts["order"].id)
            .filter(BottleLedger.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT)
            .one()
        )
        assert correction.address_group_id == delivery.address_group_id, (
            "the correction was booked to a different scope than the delivery it corrects"
        )
        assert _place(facts["stayed"].id) == Decimal("1.00")
        assert _place(facts["departed"].id) == Decimal("0.00")


# =========================================================================== #
# L3. The two columns a membership edit destroys and nothing rebuilds
# =========================================================================== #


class TestLastServedTimestampsAcrossAMembershipEdit:
    """`bottle_balances.last_delivery_at` / `last_return_at` are the only place
    facts a membership edit can DESTROY rather than move, because they are the
    only ones no later pass can re-derive: `recompute_balance_after` rewrites
    `balance_after` and `reconcile_balance` rewrites `balance`, and BOTH ignore
    these two columns entirely.

    They are not internal. `admin_ui/src/pages/BottleTracking.js` renders
    `last_delivery_at` as a COLUMN of the admin bottle-tracking table, and
    `get_customer_summary` publishes both per address on
    `GET /admin/bottles/balances/<user_id>` — the customer drawer. An operator
    chasing stale empties reads a blank "last delivery" for every place that was
    ever grouped and concludes it has never been served.

    They move no bottles, so conservation is untouched, `stored == ledger_sum` is
    untouched and the sweep is clean — which is why the entire 790-scenario
    effort has exactly one assertion mentioning `last_delivery_at`, and that one
    only checks the KEY is present on a serializer payload."""

    def test_a_JOIN_carries_both_joiners_last_served_timestamps(self, db, client, app):
        """RE-POINTED TO THE FIXED BEHAVIOUR (was: PINS THE ACTUAL OUTCOME).

        `absorb_address_into_group` now hands `last_delivery_at` /
        `last_return_at` back alongside `absorbed_balance`, and
        `_absorb_joiners_into_group` step 5 credits all three onto the place's
        single row (latest wins). The ADMIN SURFACES are asserted through the
        real routes below, because they are the reason these two columns are not
        internal: `BottleTracking.js` renders `last_delivery_at` as a table
        column and `get_customer_summary` publishes both in the customer drawer.

        `notes` is still dropped, and deliberately so: a joiner's free-text note
        describes THAT ADDRESS, not the place, and there is no defensible merge
        of two of them."""
        admin = _admin(db)
        ua, ub = _user(db), _user(db)
        a, b = _addr(db, ua.id), _addr(db, ub.id)
        _deliver(db, ua, a, "6")
        _give_back(db, ua, a, "1")
        _deliver(db, ub, b, "4")
        _give_back(db, ub, b, "1")
        served_before = {
            a.id: (_own_row(a.id).last_delivery_at, _own_row(a.id).last_return_at),
            b.id: (_own_row(b.id).last_delivery_at, _own_row(b.id).last_return_at),
        }
        assert all(all(stamps) for stamps in served_before.values())
        total_before = _stored_total()

        group = CustomerLinkService().create_place_group(
            [a.id, b.id], acting_admin_id=admin.id, reason="office"
        )

        latest_delivery = max(stamps[0] for stamps in served_before.values())
        latest_return = max(stamps[1] for stamps in served_before.values())

        place_row = _group_row(group.id)
        assert place_row.balance == Decimal("8.00")
        assert place_row.last_delivery_at == latest_delivery, (
            "a place served yesterday must not read never-served"
        )
        assert place_row.last_return_at == latest_return
        assert place_row.notes is None          # still dropped, deliberately

        # The admin surfaces render the carried date, not a blank.
        listing = client.get("/api/v1/admin/bottles/balances", headers=_headers(app, admin))
        assert listing.status_code == 200, listing.get_json()
        row = [
            item for item in listing.get_json()["data"]["items"]
            if item["address_group_id"] == group.id
        ]
        assert len(row) == 1
        assert row[0]["last_delivery_at"] is not None and row[0]["balance"] == 8.0
        drawer = client.get(
            f"/api/v1/admin/bottles/balances/{ua.id}", headers=_headers(app, admin)
        )
        assert [x["last_delivery_at"] for x in drawer.get_json()["data"]["addresses"]] != [None]

        # The two rebuild passes still ignore these columns — they neither
        # repair nor DESTROY what the join carried.
        BottleTrackingService.recompute_balance_after(BottleScope.for_group(group.id))
        BottleTrackingService().reconcile_balance(a.id)
        db.session.commit()
        db.session.expire_all()
        assert _group_row(group.id).last_delivery_at == latest_delivery

        # ...and every guard the suite has is still satisfied: no bottle moved.
        assert _stored_total() == total_before == Decimal("8.00")
        assert _group_row(group.id).balance == _group_ledger_sum(group.id)
        assert _sweep() == _CLEAN_SWEEP

    def test_a_DISSOLVE_carries_the_places_last_served_timestamps(self, db):
        """RE-POINTED TO THE FIXED BEHAVIOUR (was: PINS THE ACTUAL OUTCOME).

        `release_group_history_to_address` now INHERITS the group row's
        `last_delivery_at` / `last_return_at` onto the survivor (step 4b, latest
        wins) before step 5 deletes the row that held them."""
        place = _Place(db, member_count=2)
        _deliver(db, place.ua, place.a, "5")
        _give_back(db, place.ub, place.b, "1")
        group_row = _group_row(place.group_id)
        assert group_row.last_delivery_at is not None
        assert group_row.last_return_at is not None
        served = (group_row.last_delivery_at, group_row.last_return_at)
        total_before = _stored_total()

        place.remove(place.a, reason="moved out")
        db.session.expire_all()

        survivor_row = _own_row(place.b.id)
        assert survivor_row.balance == Decimal("4.00")
        assert (survivor_row.last_delivery_at, survivor_row.last_return_at) == served
        assert _group_row(place.group_id) is None       # the row that knew is gone
        assert _stored_total() == total_before == Decimal("4.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_membership_edit_must_carry_the_last_served_timestamps(self, db):
        """FIXED (was xfail(strict)). The round trip join -> dissolve keeps both
        columns: `absorb_address_into_group` hands them back with
        `absorbed_balance`, `_absorb_joiners_into_group` step 5 credits the
        latest of them onto the place, and `release_group_history_to_address`
        step 4b inherits the group's onto the survivor before deleting the row.
        """
        admin = _admin(db)
        ua, ub = _user(db), _user(db)
        a, b = _addr(db, ua.id), _addr(db, ub.id)
        _deliver(db, ua, a, "6")
        _give_back(db, ua, a, "1")
        _deliver(db, ub, b, "4")
        _give_back(db, ub, b, "1")
        latest_delivery = max(
            _own_row(a.id).last_delivery_at, _own_row(b.id).last_delivery_at
        )
        latest_return = max(_own_row(a.id).last_return_at, _own_row(b.id).last_return_at)

        group = CustomerLinkService().create_place_group(
            [a.id, b.id], acting_admin_id=admin.id, reason="office"
        )

        assert _group_row(group.id).last_delivery_at == latest_delivery
        assert _group_row(group.id).last_return_at == latest_return

        CustomerLinkService().remove_address_from_group(
            a.id, acting_admin_id=admin.id, reason="moved out"
        )
        db.session.expire_all()

        assert _own_row(b.id).last_delivery_at == latest_delivery
        assert _own_row(b.id).last_return_at == latest_return


# =========================================================================== #
# L4. A place with exactly ONE owner
# =========================================================================== #


class TestAPlaceWithExactlyOneOwner:
    """PINS THE ACTUAL OUTCOME (reported, not fixed).

    "A place is a SHARED place" is the rule §7.3 was written to enforce, and
    nothing in the codebase can express it. `_assert_place_group_eligible`
    (customer_link_service.py:616) checks grocery, entity and already-grouped and
    NEVER that the owners are distinct; `create_place_group` only requires
    `len(set(address_ids)) >= 2`; and `_dissolve_if_last_member` counts REMAINING
    ADDRESS ROWS, not distinct owners, so a one-person two-address place never
    triggers §7.3 and persists indefinitely.

    It is also the most natural NON-coworker use of the feature and the one an
    admin reaches for first: "Home" and "Home — back gate" at one point, whose
    empties should pool. `get_place_group_suggestions` will never propose it (it
    requires >= 2 DISTINCT customers), so it arrives only through manual admin
    action or through an account merge — both real support workflows. Every
    fixture in the whole effort builds two DIFFERENT customers; not one scenario
    constructs this.

    The consequence that reaches a human: the customer's own Delete button
    permanently 400s with a place-group error they cannot act on."""

    def test_the_admin_picker_builds_a_one_owner_place_and_nothing_ever_collapses_it(
        self, db, client, app
    ):
        admin = _admin(db)
        owner = _user(db)
        front, back = _addr(db, owner.id, "Home"), _addr(db, owner.id, "Home — back gate")
        _deliver(db, owner, front, "6")
        _deliver(db, owner, back, "2")

        response = client.post(
            "/api/v1/admin/place-groups",
            json={"addressIds": [front.id, back.id], "reason": "one house, two gates"},
            headers=_headers(app, admin),
        )

        # 1. There is no fence. The place exists, over ONE owner.
        assert response.status_code == 201, response.get_json()
        group_id = response.get_json()["data"]["place_group_id"]
        detail = CustomerLinkService().get_place_group_detail(group_id)
        assert [m["owner"]["id"] for m in detail["members"]] == [owner.id, owner.id]
        assert len({m["owner"]["id"] for m in detail["members"]}) == 1
        assert detail["place_balance"] == Decimal("8.00")
        assert _place(front.id) == _place(back.id) == Decimal("8.00")

        # 2. The customer's own screen says "shared" while naming only themselves.
        overview = client.get(
            "/api/v1/orders/bottles/my-balances", headers=_headers(app, owner)
        )
        rows = overview.get_json()["data"]["balances"]
        assert len(rows) == 1                     # one PLACE, deduplicated
        assert rows[0]["is_grouped"] is True
        assert [m["is_own"] for m in rows[0]["place_members"]] == [True]
        assert len(rows[0]["place_members"]) == 1

        # 3. ...and neither address can be deleted by the person who owns both.
        for address in (front, back):
            refusal = client.delete(
                f"/api/v1/addresses/{address.id}", headers=_headers(app, owner)
            )
            assert refusal.status_code == 400, refusal.get_json()
            assert refusal.get_json()["data"]["error_code"] == (
                "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
            )
            assert UserAddress.query.get(address.id) is not None

        # 4. Removing the FIRST of the two does dissolve it — the round trip
        #    conserves, so the shape is a support burden, not a bottle loss.
        total_before = _stored_total()
        result = CustomerLinkService().remove_address_from_group(
            front.id, acting_admin_id=admin.id, reason="ungrouped on request"
        )
        db.session.expire_all()
        assert result["dissolved"] is True
        assert _place(back.id) == Decimal("8.00")
        assert _place(front.id) == Decimal("0.00")
        assert _stored_total() == total_before == Decimal("8.00")
        assert client.delete(
            f"/api/v1/addresses/{front.id}", headers=_headers(app, owner)
        ).status_code == 200
        assert _sweep() == _CLEAN_SWEEP

    def test_attrition_leaves_a_one_owner_place_that_the_dissolve_rule_cannot_reach(self, db):
        """The second route: a three-address place where one person owns two of
        them loses its third member. `_dissolve_if_last_member` sees TWO
        remaining address rows and returns False, so §7.3 never fires and the
        place stays one-person for good."""
        admin = _admin(db)
        owner, coworker = _user(db), _user(db)
        mine_a, mine_b = _addr(db, owner.id, "Desk"), _addr(db, owner.id, "Store room")
        theirs = _addr(db, coworker.id, "Desk")
        group = CustomerLinkService().create_place_group(
            [mine_a.id, mine_b.id, theirs.id], acting_admin_id=admin.id, reason="office"
        )
        _deliver(db, owner, mine_a, "5")

        result = CustomerLinkService().remove_address_from_group(
            theirs.id, acting_admin_id=admin.id, reason="the coworker left"
        )
        db.session.expire_all()

        assert result["dissolved"] is False
        members = UserAddress.query.filter_by(address_group_id=group.id).all()
        assert sorted(a.id for a in members) == sorted([mine_a.id, mine_b.id])
        assert {a.user_id for a in members} == {owner.id}
        assert _place(mine_a.id) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_an_account_merge_collapses_a_real_place_to_one_owner_with_no_event(self, db):
        """The third route, and the one no admin ever chose:
        `CrossPlatformSyncService._transfer_user_references` (line 340) re-owns
        `UserAddress.user_id` from secondary to primary with a bulk UPDATE, so
        merging two accounts that each own an address in the SAME place collapses
        it to one owner without touching membership and without writing a single
        `CustomerLinkEvent`.

        The COLD-collection variant is used deliberately: with
        `secondary.addresses` never loaded, the terminal delete cascade
        lazy-loads it after the bulk UPDATE, finds nothing, and the address
        survives re-owned (see `TestTheAccountMergeDeletePath` for the hot
        variant, which deletes it instead)."""
        from business_app.services.cross_platform_sync_service import CrossPlatformSyncService

        admin = _admin(db)
        primary, secondary = _user(db), _user(db)
        primary.created_at = datetime.now(UTC) - timedelta(days=30)
        secondary.created_at = datetime.now(UTC)
        primary.registration_source = "web"
        secondary.registration_source = "telegram"
        db.session.commit()
        mine = _addr(db, primary.id, "Desk")
        theirs = _addr(db, secondary.id, "Desk")
        group = CustomerLinkService().create_place_group(
            [mine.id, theirs.id], acting_admin_id=admin.id, reason="office"
        )
        _deliver(db, primary, mine, "4")
        _deliver(db, secondary, theirs, "3")
        events_before = CustomerLinkEvent.query.count()
        total_before = _stored_total()

        result = CrossPlatformSyncService().auto_link_accounts(primary, secondary, "merge")
        db.session.expire_all()

        assert result["success"] is True
        members = UserAddress.query.filter_by(address_group_id=group.id).all()
        assert sorted(a.id for a in members) == sorted([mine.id, theirs.id])
        assert {a.user_id for a in members} == {primary.id}, (
            "the merge did not collapse the place onto one owner — re-check the fixture"
        )
        assert CustomerLinkEvent.query.filter_by(event_type="remove_from_place_group").count() == 0
        assert CustomerLinkEvent.query.count() >= events_before      # merge events only
        assert _place(mine.id) == Decimal("7.00")
        assert _stored_total() == total_before == Decimal("7.00")
        # And §7.3 still cannot reach it: two address rows, one person.
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# L5. One office, its whole life, checked by CROSS-SURFACE AGREEMENT
# =========================================================================== #


class TestOneOfficesWholeLife:
    """The direct answer to the per-axis blind spot.

    `test_the_whole_lifecycle_conserves_at_EVERY_step` above asserts a GLOBAL
    SUM, and a global sum is precisely the oracle a misattribution satisfies:
    move a place's bottles into a scope no address can reach and Σ is unchanged,
    every per-place `stored == ledger_sum` still holds, and the nightly sweep is
    still clean. So this arc asserts something a sum cannot express — that SIX
    independent surfaces agree to the cent about WHICH place holds what, at
    every one of fourteen steps.

    The six are the screens a human actually reads: the service's own
    `get_place_balance`, the admin bottle-tracking table, the customer's
    `/bottles` payload, the driver's customer-picker row, the staff statement,
    and the driver card's delivery anchor. Plus the admin dashboard's
    `total_bottles_out`, asserted as the sum over live scopes rather than as one
    place's number.

    Kept as its own class rather than grafted onto an existing one: it is a
    single fourteen-step narrative and splitting it would destroy the property
    it exists to check."""

    def test_every_surface_agrees_at_every_step_of_one_offices_lifetime(self, db, client, app):
        admin = _admin(db)
        driver = _user(db, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
        ua, ub, uc = _user(db), _user(db), _user(db)
        a, b, c = _addr(db, ua.id), _addr(db, ub.id), _addr(db, uc.id)
        _addr(db, ub.id, "Home")            # ub's spare, so the delete fence is the ONLY blocker
        svc, bottles = CustomerLinkService(), BottleTrackingService()
        # A live order at each door — the driver card resolves its bottle figure
        # from `order.delivery_address_id` and nothing else.
        cards = {a.id: _order(db, ua, a), b.id: _order(db, ub, b), c.id: _order(db, uc, c)}
        owners = {a.id: ua, b.id: ub, c.id: uc}
        timeline = []

        def agree(step, address, expected):
            db.session.expire_all()
            reads = _read_every_surface(
                client, app,
                admin=admin, driver=driver, address=address,
                owner=owners[address.id], order=cards[address.id],
            )
            signed = reads.pop("driver.card_signed")
            anchor = reads.pop("driver.card_anchor")
            assert signed == expected, f"{step}: driver card signed {signed} != {expected}"
            assert anchor == max(Decimal("0.00"), expected), f"{step}: driver anchor {anchor}"
            disagreeing = {k: v for k, v in reads.items() if v != expected}
            assert disagreeing == {}, f"{step}: expected {expected}, disagreeing: {disagreeing}"
            # The dashboard aggregates over PLACES, so it is checked against the
            # live scopes rather than against this one place's number.
            live_positive = sum(
                (Decimal(str(r.balance or 0)) for r in BottleBalance.query.all()
                 if Decimal(str(r.balance or 0)) > 0),
                Decimal("0.00"),
            )
            stats = BottleTrackingService.get_dashboard_stats()
            assert Decimal(str(stats["total_bottles_out"])) == live_positive, step
            _live_scope_rows_are_well_formed()
            assert _sweep() == _CLEAN_SWEEP, step
            timeline.append((step, expected))

        # -- 0. Two strangers, two separate places ------------------------------
        _deliver(db, ua, a, "4")
        _deliver(db, ub, b, "3")
        agree("00 before grouping (A)", a, Decimal("4.00"))
        agree("00 before grouping (B)", b, Decimal("3.00"))

        # -- 1. They turn out to be coworkers ----------------------------------
        group = svc.create_place_group(
            [a.id, b.id], acting_admin_id=admin.id, reason="same office 3F"
        )
        agree("01 grouped (A)", a, Decimal("7.00"))
        agree("01 grouped (B)", b, Decimal("7.00"))

        # -- 2..4. Four weeks of deliveries and returns through BOTH doors ------
        _deliver(db, ua, a, "5")
        agree("02 delivery at A", b, Decimal("12.00"))
        _give_back(db, ub, b, "2")
        agree("03 return at B", a, Decimal("10.00"))
        _deliver(db, ub, b, "3")
        agree("04 delivery at B", a, Decimal("13.00"))

        # -- 5. A standalone driver collection ---------------------------------
        bottles.record_standalone_collection(ua.id, a.id, Decimal("4"), actor_user_id=driver.id)
        db.session.commit()
        agree("05 standalone collection at A", b, Decimal("9.00"))

        # -- 6. An admin adjustment with notes ---------------------------------
        client.post(
            "/api/v1/admin/bottles/adjustment",
            json={"address_id": b.id, "adjustment": 1, "notes": "found one behind the printer"},
            headers=_headers(app, admin),
        )
        agree("06 admin adjustment", a, Decimal("10.00"))

        # -- 7. A fine, then waived — neither moves a bottle --------------------
        fine = bottles.issue_fine(
            None, a.id, Decimal("2"), Decimal("50000"), actor_user_id=admin.id
        )
        db.session.commit()
        agree("07a fine issued", a, Decimal("10.00"))
        bottles.waive_fine(fine.id, actor_user_id=admin.id)
        db.session.commit()
        agree("07b fine waived", b, Decimal("10.00"))
        assert BottleFine.query.get(fine.id).address_group_id == group.id

        # -- 8. A third coworker joins, with a merge review --------------------
        _deliver(db, uc, c, "6")
        wrong = _seed(db, c, uc, "3", notes="counted twice by mistake")
        preview = BottleTrackingService.build_merge_preview([c.id], group_id=group.id)
        assert preview["stored_balance"] == Decimal("19.00")
        assert preview["drift"] == Decimal("0.00")
        response = client.post(
            f"/api/v1/admin/place-groups/{group.id}/addresses",
            json={
                "addressIds": [c.id],
                "reason": "third desk; the double count is dropped and the shelf recounted",
                "excludedLedgerEntryIds": [wrong.id],
                "resultingBalance": 15,
                "previewEntryIds": preview["entry_ids"],
            },
            headers=_headers(app, admin),
        )
        assert response.status_code == 200, response.get_json()
        agree("08 third coworker joins with a reviewed merge (A)", a, Decimal("15.00"))
        agree("08 third coworker joins with a reviewed merge (C)", c, Decimal("15.00"))
        # §7.4's convergence property, at the one step that can break it.
        assert _group_row(group.id).balance == _group_ledger_sum(group.id) == Decimal("15.00")

        # -- 9. A delivery lands the next day ----------------------------------
        _deliver(db, uc, c, "2")
        agree("09 delivery at the new desk", b, Decimal("17.00"))

        # -- 10. One coworker leaves, taking the prefill the panel offered ------
        # The prefill is THIS member's own attributed entries, clamped to the
        # pool: +4 pre-join delivery, +5 delivery, -4 collection. The admin
        # adjustment was posted against B's address and the merge review's
        # corrections are place-level, so neither counts here.
        prefill = BottleTrackingService.suggested_bottles_leaving(group.id, a.id)
        assert prefill == Decimal("5.00")
        removal = client.delete(
            f"/api/v1/admin/place-groups/{group.id}/addresses/{a.id}",
            json={"reason": "moved to another branch", "bottlesLeaving": float(prefill)},
            headers=_headers(app, admin),
        )
        assert removal.status_code == 200, removal.get_json()
        assert removal.get_json()["data"]["dissolved"] is False
        agree("10 a coworker leaves with the prefill (the place)", b, Decimal("12.00"))
        agree("10 a coworker leaves with the prefill (the leaver)", a, Decimal("5.00"))

        # -- 11. A delivery to the DEPARTED address ----------------------------
        _deliver(db, ua, a, "3")
        agree("11 delivery to the departed address (the leaver)", a, Decimal("8.00"))
        agree("11 delivery to the departed address (the place)", c, Decimal("12.00"))

        # -- 12. The delete fence, before and after the removal ----------------
        fenced = client.delete(f"/api/v1/addresses/{b.id}", headers=_headers(app, ub))
        assert fenced.status_code == 400, fenced.get_json()
        assert fenced.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"

        # -- 13. The second coworker leaves; the place dissolves onto the last --
        dissolve = client.delete(
            f"/api/v1/admin/place-groups/{group.id}/addresses/{b.id}",
            json={"reason": "office closed"},
            headers=_headers(app, admin),
        )
        assert dissolve.status_code == 200, dissolve.get_json()
        assert dissolve.get_json()["data"]["dissolved"] is True
        agree("13 dissolved onto the last member", c, Decimal("12.00"))
        agree("13 the departed address is untouched by the dissolve", a, Decimal("8.00"))

        # -- 14. ...and the fence lets go ---------------------------------------
        released = client.delete(f"/api/v1/addresses/{b.id}", headers=_headers(app, ub))
        assert released.status_code == 200, released.get_json()
        assert UserAddress.query.get(b.id) is None

        # Fourteen agreement points, and the arc's own conservation:
        # +4 +3 +5 -2 +3 -4 +1 +6 +3 +2 +3 = 24 delivered/adjusted, less the
        # merge review's excluded 3 and its stated correction of 1 => 20.
        assert len(timeline) == 20
        assert _stored_total() == Decimal("20.00")
        assert _place(c.id) + _place(a.id) == Decimal("20.00")
        assert sorted(_live_scope_rows_are_well_formed()) == sorted([(None, a.id), (None, c.id)])
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# M. The account-merge path: a FOURTH address-delete entry point
# =========================================================================== #


class TestTheAccountMergeDeletePath:
    @staticmethod
    def _merge_with_a_grouped_address(db, *, preload_addresses=True, with_delivery=True):
        """A web account absorbing a telegram account that owns a grouped address,
        merged through the real `auto_link_accounts` entry point.

        The branch is deliberate. `auto_link_accounts` routes web+telegram to
        `_link_web_primary_telegram_secondary`, which is the ONE merge branch
        production actually reaches (`AuthService.link_web_account` and
        `complete_telegram_web_link` both call it that way). Its SAME-platform
        sibling is unreachable on a real database: `_merge_same_platform_accounts`
        sets `secondary_user.phone = None` before the delete and `users.phone` is
        NOT NULL in the migrated Postgres schema (the model says nullable, the
        migration named "make phone nullable" only altered
        `registration_method`) — verified against `pg_app`, where that branch
        dies on a NotNullViolation before it ever reaches the address cascade.

        `preload_addresses` is the trigger, and it is stated as a PRECONDITION
        rather than as something the merge does by itself: `secondary.addresses`
        has to be a LOADED collection for `cascade="all, delete-orphan"` to bite
        after a bulk UPDATE that does not refresh it. With the collection cold
        the cascade lazy-loads it AFTER the bulk UPDATE, finds nothing, and the
        address survives — see
        `test_a_merge_that_never_TOUCHED_the_collection_leaves_the_member_alone`.
        """
        from business_app.services.cross_platform_sync_service import CrossPlatformSyncService

        admin = _admin(db)
        primary = _user(db)
        primary.created_at = datetime.now(UTC) - timedelta(days=30)
        secondary = _user(db)
        secondary.created_at = datetime.now(UTC)
        primary.registration_source = "web"
        secondary.registration_source = "telegram"
        db.session.commit()

        coworker = _user(db)
        grouped = _addr(db, secondary.id)
        coworker_address = _addr(db, coworker.id)
        group = CustomerLinkService().create_place_group(
            [grouped.id, coworker_address.id], acting_admin_id=admin.id, reason="office"
        )
        if with_delivery:
            _deliver(db, secondary, grouped, "5")
        total_before = _stored_total()
        events_before = CustomerLinkEvent.query.count()

        if preload_addresses:
            assert [a.id for a in secondary.addresses] == [grouped.id]

        try:
            result = CrossPlatformSyncService().auto_link_accounts(primary, secondary, "merge")
        except Exception as exc:  # noqa: BLE001 - the pg arm asserts on this
            result = {"success": False, "raised": exc}
            db.session.rollback()
        db.session.expire_all()
        return {
            "result": result,
            "group_id": group.id,
            "grouped_id": grouped.id,
            "coworker_address_id": coworker_address.id,
            "primary_id": primary.id,
            "total_before": total_before,
            "events_before": events_before,
        }

    def test_the_account_merge_DELETES_a_place_group_member_behind_the_fence(self, db):
        """PINS THE ACTUAL OUTCOME (reported, not fixed).

        `assert_address_not_in_place_group` fences three delete entry points; this
        is a fourth. `_transfer_user_references` re-points the addresses with a
        bulk UPDATE that does not refresh the already-loaded
        `secondary_user.addresses` collection, and the terminal
        `db.session.delete(secondary_user)` then cascades onto it — removing a
        place-group member with no dissolve, no audit event, and no fence."""
        merged = self._merge_with_a_grouped_address(db)

        assert merged["result"]["success"] is True
        if UserAddress.query.get(merged["grouped_id"]) is not None:
            pytest.fail(
                "the cascade did NOT fire — re-examine the bug report before removing the xfail "
                "on test_the_account_merge_must_not_delete_a_place_group_member"
            )
        # The member simply VANISHED from the place: no dissolve, no audit event.
        assert UserAddress.query.filter_by(address_group_id=merged["group_id"]).all() != []
        assert [a.id for a in UserAddress.query.filter_by(address_group_id=merged["group_id"])] == [
            merged["coworker_address_id"]
        ]
        assert CustomerLinkEvent.query.count() == merged["events_before"]
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group"
        ).count() == 0
        # The bottles stay at the place, now reachable only by the coworker...
        assert _stored_total() == merged["total_before"] == Decimal("5.00")
        assert _place(merged["coworker_address_id"]) == Decimal("5.00")
        # ...and the departed member's ledger rows point at an address that is
        # GONE. FK-OFF ARTIFACT: `bottle_ledger.address_id` is a NOT NULL FK, so
        # real Postgres never reaches this state — it aborts the merge instead.
        # See the section-N pair for what production does.
        dangling = BottleLedger.query.filter_by(address_id=merged["grouped_id"]).all()
        assert dangling, "no ledger row referenced the deleted address — re-check the fixture"
        assert all(
            UserAddress.query.get(entry.address_id) is None for entry in dangling
        )
        # NOTHING in the nightly sweep can see this shape: the group still has a
        # member, so it is not orphaned, and there is no address-keyed row to strand.
        assert _sweep() == _CLEAN_SWEEP

    def test_a_merge_that_never_TOUCHED_the_collection_leaves_the_member_alone(self, db):
        """The other half of the pair, and the reason the bug above is a LATENT
        trap rather than a standing outage: with `secondary.addresses` never
        loaded, the delete cascade lazy-loads it AFTER `_transfer_user_references`
        has already re-pointed the rows, sees an empty collection, and deletes
        nothing. Identical inputs, identical merge, opposite outcome — so the
        trigger is precisely "somebody read the collection first", and a fix that
        expunges/refreshes it must keep THIS path working too."""
        merged = self._merge_with_a_grouped_address(db, preload_addresses=False)

        assert merged["result"]["success"] is True
        address = UserAddress.query.get(merged["grouped_id"])
        assert address is not None, "the cold-collection merge deleted the address after all"
        assert address.user_id == merged["primary_id"]
        assert address.address_group_id == merged["group_id"]
        assert sorted(
            a.id for a in UserAddress.query.filter_by(address_group_id=merged["group_id"])
        ) == sorted([merged["grouped_id"], merged["coworker_address_id"]])
        assert _stored_total() == merged["total_before"] == Decimal("5.00")
        assert _place(merged["grouped_id"]) == Decimal("5.00")
        assert _sweep() == _CLEAN_SWEEP

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG: the account-merge path is a FOURTH address-delete entry point, and it is "
            "unfenceable BY A CALL-SITE FENCE. cross_platform_sync_service.py:340 re-points the "
            "secondary's addresses with a bulk UPDATE (which does not refresh an already-loaded "
            "User.addresses collection — the same SQLAlchemy trap recorded for tryout.items) and "
            "line 216 then does db.session.delete(telegram_user); User.addresses is "
            "cascade='all, delete-orphan' (models/user.py:129), so the address rows are removed "
            "by the ORM RELATIONSHIP CASCADE during that flush — there is no db.session.delete("
            "address) statement anywhere on this path, which is exactly why the reviewer's grep "
            "found only the three fenced entry points and why calling "
            "assert_address_not_in_place_group cannot be wired in here at all. A place-group "
            "member is removed with no dissolve and no audit event. "
            "SCOPE, verified against pg_app: the cascade only fires when the collection was "
            "loaded earlier in the request (cold collection => the address survives, see the "
            "companion test), and on real Postgres it only COMPLETES for a member with no orders "
            "— with a delivered order the ORM nulls orders.delivery_address_id and "
            "ck_orders_address_required_after_pending aborts the whole merge instead (the "
            "dangling bottle_ledger.address_id asserted in the sibling pin is an FK-off SQLite "
            "artifact). Fix shape: expunge/refresh User.addresses before the delete (or re-point "
            "via the relationship), and add a before_delete guard on UserAddress — a mapper-level "
            "hook, not another call-site fence — so any future cascade path is caught too."
        ),
    )
    def test_the_account_merge_must_not_delete_a_place_group_member(self, db):
        merged = self._merge_with_a_grouped_address(db)

        address = UserAddress.query.get(merged["grouped_id"])
        assert address is not None, "the merge deleted a place-group member address"
        assert address.user_id == merged["primary_id"]
        assert address.address_group_id == merged["group_id"]
        assert _sweep() == _CLEAN_SWEEP


# =========================================================================== #
# N. Real Postgres — locking, and what SQLite structurally cannot see
# =========================================================================== #


def _in_a_second_session(pg_app, work, *, lock_timeout_ms=4000):
    """Run `work()` to completion in a SEPARATE Postgres session, then join it.

    The CALLER's transaction stays open across the call — that is how "another
    admin committed while I was mid-edit" is reproduced without a sleep. Returns
    {'value': ...} or {'error': exc}; `work` must reference ids only.
    """
    outcome = {}
    thread = _start_second_session(pg_app, work, outcome, lock_timeout_ms=lock_timeout_ms)
    thread.join(timeout=60)
    assert not thread.is_alive(), "the second session never finished — a lock was held"
    return outcome


def _start_second_session(pg_app, work, outcome, *, lock_timeout_ms=4000):
    """Start `work()` in another session WITHOUT joining, so the caller can go on
    while it blocks on a lock. The caller must join the returned thread."""

    def worker():
        with pg_app.app_context():
            from business_app import db as other

            try:
                other.session.execute(text(f"SET lock_timeout = '{lock_timeout_ms}ms'"))
                outcome["value"] = work()
                other.session.commit()
            except BaseException as exc:  # noqa: BLE001 - re-asserted by the caller
                other.session.rollback()
                outcome["error"] = exc
            finally:
                other.session.remove()

    thread = threading.Thread(target=worker, name="second-session", daemon=True)
    thread.start()
    return thread


def _fire_once(monkeypatch, owner, name, hook):
    """Patch `owner.name` so `hook(original, *a, **kw)` runs on the FIRST call only."""
    original = getattr(owner, name)
    state = {"fired": False}

    def patched(*args, **kwargs):
        if state["fired"]:
            return original(*args, **kwargs)
        state["fired"] = True
        return hook(original, *args, **kwargs)

    monkeypatch.setattr(owner, name, staticmethod(patched))
    return state


def _wait_until_a_session_is_blocked(pg_db, *, timeout=20.0):
    """Poll until another backend on THIS database is waiting on a lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = pg_db.session.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND wait_event_type = 'Lock'"
            )
        ).scalar()
        if waiting and int(waiting) >= 1:
            return True
        time.sleep(0.1)
    return False


class TestOnRealPostgres:
    def test_a_delivery_at_the_JOINER_cannot_commit_mid_join_at_all_any_more(
        self, pg_app, pg_db, monkeypatch
    ):
        """UPDATED: the interleaving this test used to construct is now IMPOSSIBLE,
        and that impossibility is the point.

        It used to release a delivery at the joining address so that it COMMITTED
        between the membership flush and the absorb's read, and assert the 3 were
        carried into the place — proving the absorb reads the joiner's figure AT
        ABSORB TIME rather than from something cached earlier in the join.

        The join now holds RUNG 1 — `addresses(joiner)` FOR NO KEY UPDATE, taken
        in `_load_addresses` before any bottle work — for the whole transaction.
        A delivery at that address takes `addresses(joiner)` FOR SHARE before it
        resolves its scope, so it BLOCKS at the mapping and cannot commit
        underneath the join at all. With the second session's `lock_timeout` it
        is cancelled; with production's absence of one it simply waits for the
        admin transaction and then resolves to the place.

        That is the deliberate cost the ladder buys, at admin frequency, and it
        is what closes the delivery-vs-absorb race: the delivery no longer
        chooses a scope that the join is about to invalidate. What must still
        hold — and is what this test now asserts — is that NOTHING IS LOST on
        either side of the block.
        """
        place = _Place(pg_db, member_count=2)
        _seed(pg_db, place.a, place.ua, "4")
        joiner_user = _user(pg_db)
        joiner = _addr(pg_db, joiner_user.id)
        _seed(pg_db, joiner, joiner_user, "5")
        stored_before = _stored_total()
        joiner_id, joiner_user_id = joiner.id, joiner_user.id

        def interfere():
            order = Order(
                user_id=joiner_user_id,
                order_number=f"ORD-LIFE-RACE-{next(_SEQ)}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("0.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                delivery_address_id=joiner_id,
                created_at=datetime.now(UTC),
            )
            _db.session.add(order)
            _db.session.flush()
            return BottleTrackingService().record_bottles_delivered(
                order.id, joiner_user_id, joiner_id, Decimal("3")
            )

        seen = {}

        def hook(original, address_id, group_id):
            # The join already holds addresses(joiner) FOR NO KEY UPDATE.
            seen["outcome"] = _in_a_second_session(pg_app, interfere, lock_timeout_ms=1500)
            return original(address_id, group_id)

        _fire_once(monkeypatch, BottleTrackingService, "absorb_address_into_group", hook)

        place.add([joiner], reason="moved in while a van was there")
        pg_db.session.expire_all()

        assert "error" in seen["outcome"], (
            "the delivery committed underneath the join — rung 1 is not being "
            f"held across the absorb: {seen['outcome']}"
        )
        assert "lock timeout" in str(seen["outcome"]["error"]).lower(), seen["outcome"]

        # The block cost nothing: the joiner's own 5 crossed intact, the
        # cancelled delivery wrote nothing, and both figures agree.
        assert _place(place.a.id) == Decimal("9.00")
        assert _own_row(joiner.id) is None
        assert _stored_total() == stored_before
        assert _group_ledger_sum(place.group_id) == Decimal("9.00")
        assert _sweep() == _CLEAN_SWEEP

    def test_a_delivery_committing_INSIDE_the_absorb_cannot_be_LOST(self, pg_app, pg_db):
        """The lost-update hazard `absorb_address_into_group`'s docstring names, at
        the only granularity that can show it: the interfering delivery is released
        BETWEEN the absorb's `SELECT ... FOR UPDATE` of the joiner's row and the
        `DELETE` of that same row, via an `after_cursor_execute` listener on the
        real engine.

        Without the `FOR UPDATE` the second session updates the row to 8, the
        absorb then deletes it while crediting the figure it read (5), and THREE
        bottles are destroyed — a shape no SQLite test can produce, because
        `with_for_update()` is a no-op there. With it, the delivery blocks and
        fails on its own `lock_timeout`, and nothing is lost either way. Both
        outcomes are accepted; what is asserted is that the bottles are still
        there afterwards.
        """
        from sqlalchemy import event
        from sqlalchemy.engine import Engine

        place = _Place(pg_db, member_count=2)
        _seed(pg_db, place.a, place.ua, "4")
        joiner_user = _user(pg_db)
        joiner = _addr(pg_db, joiner_user.id)
        _seed(pg_db, joiner, joiner_user, "5")
        stored_before = _stored_total()
        joiner_id, joiner_user_id = joiner.id, joiner_user.id
        state = {"fired": False, "outcome": {}}

        def interfere():
            order = Order(
                user_id=joiner_user_id,
                order_number=f"ORD-LIFE-LOST-{next(_SEQ)}",
                status=OrderStatus.DELIVERED,
                subtotal=Decimal("0.00"),
                delivery_fee=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                loyalty_discount=Decimal("0.00"),
                total_amount=Decimal("0.00"),
                delivery_address_id=joiner_id,
                created_at=datetime.now(UTC),
            )
            _db.session.add(order)
            _db.session.flush()
            return BottleTrackingService().record_bottles_delivered(
                order.id, joiner_user_id, joiner_id, Decimal("3")
            )

        def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
            # The absorb's own read of the joiner's address-keyed row: the only
            # statement in this flow selecting an ADDRESS scope with a row lock.
            state.setdefault("seen", []).append(statement)
            if state["fired"] or "bottle_balances" not in statement:
                return
            if "SELECT" not in statement or "address_group_id IS NULL" not in statement:
                return
            state["fired"] = True
            state["statement"] = statement
            state["outcome"] = _in_a_second_session(pg_app, interfere, lock_timeout_ms=1500)

        # Listened on the Engine CLASS, not on one instance: the session's engine
        # is resolved lazily per app context, and a listener bound to the wrong
        # instance would silently never fire.
        event.listen(Engine, "after_cursor_execute", after_cursor_execute)
        try:
            place.add([joiner], reason="moved in while a van was at the door")
        finally:
            event.remove(Engine, "after_cursor_execute", after_cursor_execute)
        pg_db.session.expire_all()

        assert state["fired"], [s for s in state.get("seen", []) if "bottle_balances" in s]
        assert "FOR UPDATE" in state["statement"], (
            "the absorb read the joiner's figure WITHOUT a row lock: "
            f"{state['statement']}"
        )
        if "error" in state["outcome"]:
            # The delivery was held off by the absorb's row lock and rolled back.
            assert "lock timeout" in str(state["outcome"]["error"]).lower()
            assert _stored_total() == stored_before == Decimal("9.00")
            assert _place(place.a.id) == Decimal("9.00")
        else:
            # It committed — then its three bottles MUST still exist somewhere.
            assert _stored_total() == stored_before + Decimal("3.00")
            assert _place(place.a.id) == Decimal("12.00")
        assert _own_row(joiner.id) is None
        assert _sweep() == _CLEAN_SWEEP

    def test_two_concurrent_joins_of_ONE_address_serialise_on_the_addresses_row(
        self, pg_app, pg_db, monkeypatch
    ):
        """Two joins want each other's group row but never each other's address
        row; what serialises them is step 1's membership flush. Exactly one wins,
        and the joiner's balance is absorbed exactly ONCE."""
        first = _funded_place(pg_db, "4", member_count=2)
        second = _funded_place(pg_db, "6", member_count=2)
        joiner_user = _user(pg_db)
        joiner = _addr(pg_db, joiner_user.id)
        _seed(pg_db, joiner, joiner_user, "5")
        stored_before = _stored_total()
        second_group_id, joiner_id, admin_id = second.group_id, joiner.id, second.admin.id
        seen = {}

        def interfere():
            return CustomerLinkService().add_addresses_to_group(
                second_group_id, [joiner_id], acting_admin_id=admin_id, reason="the other admin"
            )

        def hook(original, address_id, group_id):
            seen["outcome"] = _in_a_second_session(pg_app, interfere, lock_timeout_ms=1500)
            return original(address_id, group_id)

        _fire_once(monkeypatch, BottleTrackingService, "absorb_address_into_group", hook)

        first.add([joiner], reason="one admin")
        pg_db.session.expire_all()

        assert "error" in seen["outcome"], seen["outcome"]
        pg_db.session.expire_all()
        assert UserAddress.query.get(joiner.id).address_group_id == first.group_id
        assert _place(first.a.id) == Decimal("9.00")            # absorbed exactly once
        assert _place(second.a.id) == Decimal("6.00")
        assert _own_row(joiner.id) is None
        assert _stored_total() == stored_before
        assert CustomerLinkEvent.query.filter_by(event_type="add_to_place_group").count() == 1
        assert _sweep() == _CLEAN_SWEEP

    # -- Two removals from a TWO-member place ---------------------------------- #

    @staticmethod
    def _race_two_removals_from_a_two_member_place(pg_app, pg_db, monkeypatch):
        """Both admins remove a different member of a two-member place, each
        seeing exactly one member left. The second session is started and left
        BLOCKED while the first one carries on — the real production interleaving.
        Returns (place, seen)."""
        place = _funded_place(pg_db, "7", member_count=2)
        admin_id, second_address_id = place.admin.id, place.b.id
        seen = {
            "stored_before": _stored_total(),
            "second": {},
            "a": place.a.id,
            "b": place.b.id,
            "group": place.group_id,
        }

        def interfere():
            return CustomerLinkService().remove_address_from_group(
                second_address_id, acting_admin_id=admin_id, reason="the other admin"
            )

        def hook(original, **kwargs):
            thread = _start_second_session(
                pg_app, interfere, seen["second"], lock_timeout_ms=20000
            )
            seen["thread"] = thread
            # Let the second session get as far as it can before this one moves.
            _wait_until_a_session_is_blocked(pg_db)
            return original(**kwargs)

        _fire_once(monkeypatch, CustomerLinkService, "_dissolve_if_last_member", hook)
        try:
            seen["first"] = {"value": place.remove(place.a, reason="one admin")}
        except BaseException as exc:  # noqa: BLE001 - the deadlock victim may be either side
            pg_db.session.rollback()
            seen["first"] = {"error": exc}
        thread = seen.get("thread")
        if thread is not None:
            thread.join(timeout=60)
            assert not thread.is_alive(), "the second removal never finished"
        pg_db.session.rollback()
        pg_db.session.expire_all()
        return place, seen

    def test_two_concurrent_removals_from_a_two_member_place_REFUSE_THE_LOSER_BY_NAME(
        self, pg_app, pg_db, monkeypatch
    ):
        """RE-POINTED. This used to pin the pre-ladder outcome — a raw database
        collision — with `assert not isinstance(error, ValidationError)` and the
        comment "a named fence would be the FIX, not the bug". The named fence
        landed, so the pin now asserts it, and asserts it HARDER than the old one
        did: the loser must carry a §13 `error_code` the admin panel can render,
        not merely be some exception whose text happens to mention a lock.

        WHAT CHANGED IN THE SERVICE. `remove_address_from_group` now climbs the
        ladder at the START — rung 0 `address_groups(G)`, then rung 1 the WHOLE
        member set `ORDER BY id FOR NO KEY UPDATE` in one statement — BEFORE the
        membership pointer is flushed. So the two removals SERIALISE on the group
        instead of colliding inside `_dissolve_if_last_member`, and neither side
        can decide it is the last member out from a figure the other is already
        changing.

        BOTH NAMED OUTCOMES ARE CORRECT, and which one appears is a matter of how
        long the winner holds the ladder — deliberately not asserted, because the
        service's own header (`remove_address_from_group`, steps 4 and the
        `_apply_scope_lock_timeout` note) specifies exactly these two:

        * `PLACE_GROUP_NOT_FOUND` — the loser WAITED, woke on the winner's
          committed world, re-validated against the pinned member set and found
          its address is no longer in G;
        * `BOTTLE_SCOPE_LOCK_TIMEOUT` — the loser was CANCELLED while waiting
          (this test gives the second session a 20s `lock_timeout`; an operator
          may set one in production), and `_raise_place_busy` converts the 55P03
          into the same retryable refusal the driver side of this contention
          already gets, instead of a raw `OperationalError` rendered as a 500.

        What must NEVER appear again is the third outcome this test was written
        for: a `LockNotAvailable`/deadlock escaping as a non-ValidationError.
        `test_..._must_not_fail_at_the_DATABASE` states that invariant on its own;
        this test additionally pins the winner's whole end state.

        `with_for_update()` is a no-op on SQLite, so none of this is visible there.
        """
        _place_obj, seen = self._race_two_removals_from_a_two_member_place(
            pg_app, pg_db, monkeypatch
        )
        first, second = seen["first"], seen["second"]

        losers = [side for side in (first, second) if "error" in side]
        assert len(losers) == 1, (
            f"expected exactly one refused loser, got first={first}, second={second}"
        )
        error = losers[0]["error"]
        assert isinstance(error, ValidationError), (
            f"the loser must be refused by NAME, not fail at the database: {error!r}"
        )
        assert getattr(error, "error_code", None) in {
            "PLACE_GROUP_NOT_FOUND",
            "BOTTLE_SCOPE_LOCK_TIMEOUT",
        }, (
            "the refusal must carry a code the admin panel can render; got "
            f"{getattr(error, 'error_code', None)!r} for {error!r}"
        )

        # Whoever lost, no bottles were minted or destroyed...
        assert _stored_total() == seen["stored_before"] == Decimal("7.00")
        # ...and the place was not dissolved twice (one episode's pair at most).
        pair_keys = {
            entry.idempotency_key.rsplit(":", 1)[0]
            for entry in BottleLedger.query.filter(
                BottleLedger.idempotency_key.like("place_dissolve:%")
            ).all()
        }
        assert len(pair_keys) <= 1, pair_keys

        if "error" in second:
            # The expected victim (only the second session carries a lock_timeout):
            # the first removal's dissolve is intact and the loser left no trace.
            assert first["value"]["dissolved"] is True
            assert UserAddress.query.filter_by(address_group_id=seen["group"]).count() == 0
            assert _place(seen["b"]) == Decimal("7.00")
            assert _place(seen["a"]) == Decimal("0.00")
            assert _group_row(seen["group"]) is None
            assert _sweep() == _CLEAN_SWEEP
        else:
            # The other victim ordering. Under the ladder the FIRST session takes
            # rungs 0/1 before the second is even started, so this branch should
            # now be unreachable; it is kept rather than asserted away because
            # neither removal is entitled to assume it wins, and whichever one
            # did must still have emptied or halved the place — never left it
            # with both members and a released history.
            assert UserAddress.query.filter_by(address_group_id=seen["group"]).count() <= 1

    def test_two_concurrent_removals_from_a_two_member_place_must_not_fail_at_the_DATABASE(
        self, pg_app, pg_db, monkeypatch
    ):
        """Two admins working the same office must not produce a database-level
        failure: either both removals settle, or the loser is refused with a NAMED
        ValidationError the panel can render.

        WAS a strict xfail. Two things closed it, and only the second was still
        missing when this was written:

        * the ladder — `remove_address_from_group` takes rung 0
          (`address_groups`) and rung 1 (the whole member set, ascending id, one
          statement) at the START, before the membership pointer is flushed, so
          the two removals SERIALISE and the loser re-reads a pinned set rather
          than deciding on stale numbers that it, too, is the last member out;
        * the WAITER's bound and its CONVERSION. Serialising means the loser
          WAITS, and a wait that is cancelled — by this test's `lock_timeout`,
          or by an operator's — surfaced as a raw psycopg2 `LockNotAvailable`,
          which no `except ValidationError` catches and the route renders as a
          500. It is now `BOTTLE_SCOPE_LOCK_TIMEOUT` (see
          `CustomerLinkService._raise_place_busy`), the same named, retryable
          answer the driver side of this contention already gets.

        Nothing here asserts WHICH side loses: both orderings are legitimate and
        the test says so by iterating over both.
        """
        _place_obj, seen = self._race_two_removals_from_a_two_member_place(
            pg_app, pg_db, monkeypatch
        )

        for side in (seen["first"], seen["second"]):
            error = side.get("error")
            if error is not None:
                assert isinstance(error, ValidationError), f"database-level failure: {error!r}"

    # -- The account merge, on the database production actually runs ----------- #

    def test_the_merge_cascade_is_REFUSED_by_postgres_when_the_member_has_orders(self, pg_db):
        """PINS THE ACTUAL OUTCOME on the real schema (reported, not fixed).

        The SQLite pin in section M ends with a deleted address and
        `bottle_ledger` rows pointing at it — a state Postgres will not enter.
        `UserAddress` has no delete cascade to `orders`, so the ORM de-associates
        instead and NULLs `orders.delivery_address_id`, which
        `ck_orders_address_required_after_pending` refuses. So the production
        blast radius for a member WITH deliveries is not a silent vanishing: it
        is a hard failure of the whole merge.

        Worse than a clean rollback, and the second thing this pins:
        `auto_link_accounts`' own `except` block is supposed to convert any
        failure into `{'success': False, 'error': ...}`, and here it does not —
        the exception escapes the service (the handler touches the failed
        session before rolling it back), so the caller gets a 500 rather than
        the friendly refusal `AuthService.link_web_account` is written to raise.
        """
        merged = TestTheAccountMergeDeletePath._merge_with_a_grouped_address(pg_db)

        assert merged["result"]["success"] is False
        raised = merged["result"].get("raised")
        assert raised is not None, "auto_link_accounts swallowed it — re-read the bug report"
        assert "ck_orders_address_required_after_pending" in str(raised), repr(raised)
        # Nothing moved: the member is still a member and the place still holds 5.
        address = UserAddress.query.get(merged["grouped_id"])
        assert address is not None
        assert address.address_group_id == merged["group_id"]
        assert sorted(
            a.id for a in UserAddress.query.filter_by(address_group_id=merged["group_id"])
        ) == sorted([merged["grouped_id"], merged["coworker_address_id"]])
        assert _stored_total() == merged["total_before"] == Decimal("5.00")
        assert _place(merged["grouped_id"]) == Decimal("5.00")
        # ...and no ledger row was left pointing at a deleted address.
        assert all(
            UserAddress.query.get(e.address_id) is not None for e in BottleLedger.query.all()
        )
        assert _sweep() == _CLEAN_SWEEP

    def test_the_merge_cascade_DOES_delete_an_orderless_place_member_on_postgres(self, pg_db):
        """PINS THE ACTUAL OUTCOME on the real schema (reported, not fixed).

        Strip the deliveries and no constraint stands in the way any more: the
        cascade deletes a live place-group member on PRODUCTION Postgres, the
        merge reports success, and the place quietly loses a member with no
        dissolve and no audit event. This is the shape the fix has to cover —
        the SQLite pin above is the same defect, just with an extra FK-off
        embellishment the real database would have blocked.
        """
        merged = TestTheAccountMergeDeletePath._merge_with_a_grouped_address(
            pg_db, with_delivery=False
        )

        assert merged["result"]["success"] is True
        assert UserAddress.query.get(merged["grouped_id"]) is None
        assert [
            a.id for a in UserAddress.query.filter_by(address_group_id=merged["group_id"])
        ] == [merged["coworker_address_id"]]
        assert CustomerLinkEvent.query.count() == merged["events_before"]
        assert CustomerLinkEvent.query.filter_by(
            event_type="remove_from_place_group"
        ).count() == 0
        assert _sweep() == _CLEAN_SWEEP
