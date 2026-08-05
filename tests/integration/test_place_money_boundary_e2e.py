"""THE MONEY BOUNDARY STAYS EXACTLY WHERE IT IS.

The 2026-07-27 place re-key moved bottle balances from ``(user_id, address_id)``
to the PLACE (the address group when ``addresses.address_group_id`` is set, else
the address itself). This file proves that re-key never reached the allocation
engine.

One claim, asserted from every angle:

    NO bottle operation and NO place-lifecycle mutation — grouping, ungrouping,
    a ``bottles_leaving`` split, a dissolve, a merge review with a backfill /
    exclusion / override, a fine issued, paid or waived, an admin adjustment, a
    reconcile — may change ONE stored money value.

Money reads MAY change, and exactly one input is allowed to change them:
PLACE MEMBERSHIP. ``get_place_cod_statement`` and the COD cap's place arm are
membership-driven views over payments that themselves never move. Every test
below that watches a money read change proves the change is attributable to
membership and to nothing else — most sharply by holding membership fixed and
varying the bottle figures wildly, which must produce an identical result.

HOW THIS FILE CANNOT PASS VACUOUSLY
Every "money is unchanged" assertion runs through ONE oracle,
``money_snapshot``, which reads every ``payments`` / ``orders`` /
``cash_collection_events`` / ``cash_collection_allocations`` /
``driver_cash_sessions`` row in the database and stringifies every field that
carries value. ``TestSnapshotOracleIsFalsifiable`` posts a REAL collection
through ``CashCollectionService.post_collection`` and asserts the oracle SEES
it. If the oracle ever goes blind — wrong query, expired session, empty filter
— that test goes red first and the rest of the file's green is not trusted.

Conservation is always asserted as a PAIR (total before, total after), never as
one side, because minting and destroying are both failures and a one-sided
assertion catches neither reliably.

CONSERVATION ALONE IS NOT ENOUGH, and this file says so with a test. The
fine-after-a-dissolve defect moves BOTH global totals by exactly -6 — it is
perfectly conserving — while booking the six bottles into a scope no address can
resolve to. Every conservation assertion here is therefore paired with a
per-SCOPE attribution assertion (``ledger_sum_for_place`` /
``ledger_sum_for_group`` / ``get_place_balance``), because a global sum is blind
to exactly this class of bug.

Tests named ``test_TODAY_*`` pin CURRENT, WRONG behaviour with exact numbers
next to the strict xfail that states the intended contract; the xfail stops at
its first violated assertion, so without them most of the damage is described
but never executed. They must be deleted by whatever change fixes the defect.

Deliberately NOT modified by this file: the money engine and its tests. This
axis only observes.

Test-infrastructure note: the default backend is in-memory SQLite with FOREIGN
KEYS OFF and ``with_for_update()`` as a NO-OP, so nothing here proves FK
integrity or lock ordering. The two Postgres-backed cases at the bottom use
``pg_app`` / ``pg_db`` for exactly that reason.
"""

from datetime import datetime, UTC
from decimal import Decimal

import pytest

from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
)
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order, OrderItem
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    DriverCashSession,
    Payment,
)
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.order_service import OrderService
from business_app.services.staff_service import StaffService
from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserStatus,
    UserType,
)
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_user,
)


pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# THE ORACLE
# --------------------------------------------------------------------------- #

_PAYMENT_FIELDS = (
    "user_id",
    "order_id",
    "subscription_id",
    "payment_method",
    "status",
    "amount",
    "currency",
    "amount_collected",
    "outstanding_amount",
    "paid_at",
    "collected_by",
    "last_collected_at",
    "provider_data",
    "provider_transaction_id",
    "failure_reason",
)

_ORDER_FIELDS = (
    "user_id",
    "status",
    "total_amount",
    "subtotal",
    "delivery_fee",
    "discount_amount",
    "loyalty_discount",
    "is_paid",
    "payment_method",
    "delivery_address_id",
)

_EVENT_FIELDS = (
    "customer_id",
    "collector_user_id",
    "recorded_by_user_id",
    "order_id",
    "delivery_id",
    "driver_cash_session_id",
    "amount",
    "currency",
    "source",
    "unapplied_amount",
    "voided_at",
    "void_reason",
    "scope_type",
    "scope_snapshot",
)

_ALLOCATION_FIELDS = (
    "cash_collection_event_id",
    "payment_id",
    "order_id",
    "allocated_amount",
    "allocation_order",
    "allocation_mode",
    "reversed_at",
    "reversal_reason",
    "source_customer_id",
    "beneficiary_user_id",
)

_CASH_SESSION_FIELDS = (
    "driver_user_id",
    "status",
    "expected_cash",
    "gross_cash_collected",
    "expected_cash_on_hand",
    "declared_cash",
    "verified_cash",
    "declared_variance",
    "verified_variance",
    "blocked_from_cod",
)


def _stringify(value):
    """Everything as a comparable string so Decimal/float/enum shape shifts show."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return repr(value)
    if hasattr(value, "value"):  # Enum
        return str(value.value)
    return str(value)


def _rows(model, fields):
    return {
        row.id: {name: _stringify(getattr(row, name)) for name in fields}
        for row in model.query.order_by(model.id.asc()).all()
    }


def money_snapshot(db):
    """EVERY stored money value in the database, as comparable strings.

    Deliberately unfiltered: an assertion that only looked at "the payments this
    test made" would miss a write that landed on a coworker's row, which is the
    exact damage this axis exists to rule out.

    ``expire_all`` first, so the snapshot reads the DATABASE and not a stale
    identity map — several place-lifecycle paths bulk-UPDATE with
    ``synchronize_session=False`` and would otherwise leave pre-move column
    values in memory for the oracle to happily re-confirm.
    """
    db.session.expire_all()
    return {
        "payments": _rows(Payment, _PAYMENT_FIELDS),
        "orders": _rows(Order, _ORDER_FIELDS),
        "events": _rows(CashCollectionEvent, _EVENT_FIELDS),
        "allocations": _rows(CashCollectionAllocation, _ALLOCATION_FIELDS),
        "cash_sessions": _rows(DriverCashSession, _CASH_SESSION_FIELDS),
    }


def _force_one_member_place(db, address_id, group_id):
    """Point ONE address at a group WITHOUT the service, and move its bottles with it.

    No service path builds a one-member place any more: `create_place_group`
    requires >= 2, a removal that would leave one member DISSOLVES in the same
    transaction, and a memberless group is refused as a join target
    (`PLACE_GROUP_DISSOLVED`). The shape survives in data written before that
    refusal, and `_dissolve_if_last_member`'s ZERO-REMAINING arm exists for it —
    which is why `release_group_history_to_address` must pass
    `allow_memberless=True`. Building it by hand keeps that arm covered.
    """
    db.session.query(UserAddress).filter(UserAddress.id == address_id).update(
        {UserAddress.address_group_id: group_id}, synchronize_session=False
    )
    db.session.query(BottleLedger).filter(
        BottleLedger.address_id == address_id, BottleLedger.address_group_id.is_(None)
    ).update({BottleLedger.address_group_id: group_id}, synchronize_session=False)
    own = BottleBalance.query.filter_by(
        address_id=address_id, address_group_id=None
    ).one_or_none()
    carried = Decimal(str(own.balance or 0)) if own is not None else Decimal("0.00")
    if own is not None:
        db.session.delete(own)
    db.session.flush()
    db.session.add(BottleBalance(address_group_id=group_id, balance=carried))
    db.session.commit()
    db.session.expire_all()


def bottle_totals(db):
    """(Σ every bottle_balances row, Σ every bottle_ledger quantity).

    The pair conservation is asserted against. A single figure cannot tell a
    mint from a move.
    """
    db.session.expire_all()
    balances = sum(
        (Decimal(str(b.balance or 0)) for b in BottleBalance.query.all()), Decimal("0.00")
    )
    ledger = sum(
        (Decimal(str(e.quantity or 0)) for e in BottleLedger.query.all()), Decimal("0.00")
    )
    return balances, ledger


def ledger_sum_for_place(address_id):
    scope = BottleTrackingService.resolve_scope(address_id)
    return sum(
        (
            Decimal(str(e.quantity or 0))
            for e in BottleLedger.query.filter(*scope.ledger_filter()).all()
        ),
        Decimal("0.00"),
    )


def ledger_sum_for_group(group_id):
    return sum(
        (
            Decimal(str(e.quantity or 0))
            for e in BottleLedger.query.filter(BottleLedger.address_group_id == group_id).all()
        ),
        Decimal("0.00"),
    )


def error_code_of(response):
    """`error_code` off an error envelope, wherever this API puts it.

    Derived from the real payload rather than hard-coded to one nesting: the
    admin routes and the `@handle_api_exception` routes render it at different
    depths, and a hand-copied path goes stale silently.
    """
    body = response.get_json() or {}
    if "error_code" in body:
        return body["error_code"]
    return ((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get(
        "error_code"
    )


class MoneyFreeze:
    """Context manager: nothing in the money engine moved while this ran."""

    def __init__(self, db, label=""):
        self.db = db
        self.label = label

    def __enter__(self):
        self.before = money_snapshot(self.db)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            # Still assert on an expected raise — a rejected call must leave
            # nothing flushed for the next commit on this session to adopt.
            self.db.session.rollback()
        after = money_snapshot(self.db)
        assert after == self.before, (
            f"MONEY BOUNDARY CROSSED{' during ' + self.label if self.label else ''}: "
            f"before={self.before} after={after}"
        )
        return False


# --------------------------------------------------------------------------- #
# FIXTURE BUILDERS — real service write paths only
# --------------------------------------------------------------------------- #


def staff_user(db, role=UserRole.DELIVERY_DRIVER):
    n = int(datetime.now(UTC).timestamp() * 1000) % 100000
    user = User(
        email=f"staff{n}-{id(db) % 9973}@example.com",
        phone=f"+99877{n:06d}",
        password_hash=hash_password("StaffPassword123!"),
        first_name="S",
        last_name="T",
        user_type=UserType.STAFF,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def token_for(app, user):
    from flask_jwt_extended import create_access_token

    with app.app_context():
        return {
            "Authorization": f"Bearer {create_access_token(identity=str(user.id))}",
            "Content-Type": "application/json",
        }


def group_addresses(db, admin, *addresses, reason="office", label="office", **review):
    """Create a place through the REAL service path (never by hand-setting FKs)."""
    return CustomerLinkService().create_place_group(
        [a.id for a in addresses],
        acting_admin_id=admin.id,
        reason=reason,
        label=label,
        **review,
    )


def seed_place_bottles(db, admin, address, quantity, notes="seed"):
    """Put bottles at a place through ``admin_adjust_balance`` — a real writer."""
    return BottleTrackingService().admin_adjust_balance(
        user_id=None,
        address_id=address.id,
        adjustment=Decimal(str(quantity)),
        actor_user_id=admin.id,
        notes=notes,
    )


def seed_unapplied_credit(db, user, amount, admin):
    """Park `amount` of unapplied prepaid credit via a REAL over-collection."""
    order, _payment = delivered_cod_order(db, user, total=Decimal("1000.00"))
    return CashCollectionService().post_collection(
        customer_id=user.id,
        amount=Decimal("1000.00") + Decimal(str(amount)),
        source="standalone_meeting",
        order_id=order.id,
        recorded_by_user_id=admin.id,
        notes="over-collection seeding credit",
    )


def bottle_product(db, per_unit="2"):
    from business_app.models.product import Product, ProductCategory

    category = ProductCategory(name=f"Water-{id(db) % 9973}", description="w", is_active=True)
    db.session.add(category)
    db.session.commit()
    product = Product(
        name="Pure Water 19L",
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


def cash_order_with_bottles(db, user, product, address, *, quantity, status, total="30000.00"):
    """A CASH order with returnable-bottle items AND its COD payment row."""
    n = BottleLedger.query.count() + Order.query.count() + 1
    order = Order(
        user_id=user.id,
        order_number=f"ORD-MB-{n}-{id(address) % 9973}",
        status=status,
        subtotal=Decimal(total),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal(total),
        payment_method=PaymentMethod.CASH,
        delivery_address_id=address.id,
        created_at=datetime.now(UTC),
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * Decimal(str(quantity)),
        )
    )
    payment = Payment(
        order_id=order.id,
        user_id=user.id,
        payment_method=PaymentMethod.CASH,
        amount=Decimal(total),
        currency="UZS",
        status=PaymentStatus.PENDING,
        payment_id=f"pay-mb-{order.id}",
        amount_collected=Decimal("0.00"),
        outstanding_amount=Decimal(total),
        created_at=datetime.now(UTC),
    )
    db.session.add(payment)
    db.session.commit()
    return order, payment


def open_cash_session(db, driver, expected="45000.00"):
    session = DriverCashSession(
        session_id=f"dcs-{driver.id}",
        driver_user_id=driver.id,
        expected_cash=Decimal(expected),
        gross_cash_collected=Decimal("0.00"),
        expected_cash_on_hand=Decimal(expected),
        session_started_at=datetime.now(UTC),
    )
    db.session.add(session)
    db.session.commit()
    return session


def open_bottle_session(db, driver, loaded=100):
    return BottleTrackingService().open_bottle_session(
        driver_user_id=driver.id, bottles_loaded=loaded, actor_user_id=driver.id
    )


# --------------------------------------------------------------------------- #
# 1. THE ORACLE MUST BE ABLE TO SEE A MONEY CHANGE
# --------------------------------------------------------------------------- #


class TestSnapshotOracleIsFalsifiable:
    def test_money_snapshot_detects_a_real_cash_collection(self, db):
        """Every other assertion in this file is 'the snapshot is identical'.

        If ``money_snapshot`` silently read nothing, all of them would pass
        vacuously. Post a REAL collection and require the oracle to see it —
        in the payments table, in the events table AND in the allocations table,
        so a partially-blind oracle fails too.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, quiet = make_address(db, u1), make_address(db, u2), make_address(db, u1)
        group_addresses(db, admin, a1, a2, quiet)
        order1, payment1 = delivered_cod_order(db, u1, address=a1, total=Decimal("20000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("15000.00"))

        before = money_snapshot(db)
        assert before["payments"], "oracle read zero payments — it is blind"

        CashCollectionService().post_collection(
            customer_id=u1.id,
            amount=Decimal("20000.00"),
            source="standalone_meeting",
            order_id=order1.id,
            recorded_by_user_id=admin.id,
            notes="oracle falsifiability probe",
        )

        after = money_snapshot(db)
        assert after != before, "money_snapshot cannot detect a real collection"
        assert after["payments"][payment1.id] != before["payments"][payment1.id]
        assert len(after["events"]) == len(before["events"]) + 1
        assert len(after["allocations"]) > len(before["allocations"])
        # ATTRIBUTION, not just the global delta: an exact-cover collection at a
        # shared place settles the payer's OWN order and leaves the coworker's
        # debt byte-identical. A global "something changed" assertion is blind to
        # a write that lands on the wrong person's row, which is the whole class
        # of damage this file exists to rule out.
        coworker_payment_id = (
            Payment.query.filter_by(user_id=u2.id).one().id
        )
        assert after["payments"][coworker_payment_id] == before["payments"][coworker_payment_id]
        assert after["payments"][payment1.id]["outstanding_amount"] == "0.00"
        assert after["payments"][payment1.id]["amount_collected"] == "20000.00"

    def test_bottle_totals_detects_a_real_bottle_movement(self, db):
        """The conservation oracle must be able to see a bottle move too."""
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)

        before = bottle_totals(db)
        seed_place_bottles(db, admin, a1, 7)
        after = bottle_totals(db)
        assert after == (before[0] + Decimal("7"), before[1] + Decimal("7"))

    def test_money_freeze_fires_when_a_lifecycle_call_moves_money(self, db):
        """POSITIVE CONTROL for every ``MoneyFreeze`` block in this file.

        Every lifecycle assertion here is 'wrap the real service call and require
        the money snapshot to be identical'. That is only meaningful if the
        wrapper would actually TRIP on a money write committed on the same
        session, inside the same block, around the same call. So: run the real
        ``remove_address_from_group``, and alongside it commit exactly the kind
        of 'tidy up the departing member's debts on ungroup' write §8 retired.

        ``MoneyFreeze`` must raise. If a refactor of the oracle (a narrowed
        query, a dropped ``expire_all``, a stale identity map) ever made it
        blind, this test goes red and the file's green stops being trusted.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 5)
        _order, payment = delivered_cod_order(db, u1, address=a1, total=Decimal("30000.00"))

        with pytest.raises(AssertionError, match="MONEY BOUNDARY CROSSED"):
            with MoneyFreeze(db, "positive control"):
                CustomerLinkService().remove_address_from_group(
                    a1.id, acting_admin_id=admin.id, reason="left",
                    bottles_leaving=Decimal("2"),
                )
                # The forbidden write, committed on the same session the
                # lifecycle call just used.
                Payment.query.get(payment.id).outstanding_amount = Decimal("0.00")
                db.session.commit()


# --------------------------------------------------------------------------- #
# 2. DELIVERY / RETURN AT A SHARED PLACE
# --------------------------------------------------------------------------- #


class TestDeliveryAtSharedPlace:
    def test_delivered_transition_moves_bottles_and_no_money_row(self, db):
        """A DELIVERED transition at a grouped address books bottles to the
        PLACE and leaves every money row — including the coworker's open COD
        debt — byte-identical.

        No prepaid credit and no reservation exist here, so the money engine
        has nothing of its own to settle: any money delta at all is a boundary
        crossing. The delivered ORDER's own status row is the one thing that
        legitimately moves, and it is excluded explicitly rather than by a
        blanket 'orders may change' relaxation.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        product = bottle_product(db, per_unit="2")
        order, _payment = cash_order_with_bottles(
            db, u1, product, a1, quantity=3, status=OrderStatus.CONFIRMED
        )

        place_before = BottleTrackingService.get_place_balance(a1.id)
        before = money_snapshot(db)

        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)

        after = money_snapshot(db)
        assert BottleTrackingService.get_place_balance(a1.id) == place_before + Decimal("6.00")
        # Every payment except the delivered order's own is byte-identical...
        own_payment_id = Payment.query.filter_by(order_id=order.id).one().id
        assert {k: v for k, v in after["payments"].items() if k != own_payment_id} == {
            k: v for k, v in before["payments"].items() if k != own_payment_id
        }
        # ...and the delivered order's own payment moved no VALUE. The money
        # engine's settlement step stamps `cod_prepayment_reserved_amount: 0.0`
        # into provider_data even when there is nothing to consume; that is the
        # engine's own bookkeeping, not a bottle-derived quantity, and every
        # figure that represents money is unchanged.
        own_before = before["payments"][own_payment_id]
        own_after = after["payments"][own_payment_id]
        assert {k: v for k, v in own_after.items() if k != "provider_data"} == {
            k: v for k, v in own_before.items() if k != "provider_data"
        }
        db.session.expire_all()
        assert Payment.query.get(own_payment_id).provider_data == {
            "cod_prepayment_reserved_amount": 0.0
        }
        assert after["events"] == before["events"]
        assert after["allocations"] == before["allocations"]
        assert after["cash_sessions"] == before["cash_sessions"]
        # The only order that moved is the delivered one, and only its status.
        moved = {
            oid: (before["orders"][oid], after["orders"][oid])
            for oid in after["orders"]
            if before["orders"].get(oid) != after["orders"][oid]
        }
        assert set(moved) == {order.id}
        old, new = moved[order.id]
        assert {k: v for k, v in new.items() if k != "status"} == {
            k: v for k, v in old.items() if k != "status"
        }
        assert new["status"] == OrderStatus.DELIVERED.value

    def test_delivered_transition_money_outcome_is_blind_to_the_bottle_figure(self, db):
        """The strongest form of this axis: hold the money fixture IDENTICAL and
        vary the place's bottle figure wildly. The money outcome of a DELIVERED
        transition must be byte-identical in both worlds.

        This is what separates 'grouping changed the money' from 'the money
        engine did its own job'. A 'charge the customer for unreturned bottles
        at delivery' shortcut fails here and cannot fail anywhere cheaper.
        """
        outcomes = []
        for seeded_bottles in ("0", "500"):
            # Two independent fixtures in the same DB — distinct users, so the
            # money rows cannot influence each other.
            admin = make_user(db)
            u1, u2 = make_user(db), make_user(db)
            a1, a2 = make_address(db, u1), make_address(db, u2)
            group_addresses(db, admin, a1, a2)
            if seeded_bottles != "0":
                seed_place_bottles(db, admin, a2, seeded_bottles)
            product = bottle_product(db, per_unit="2")
            order, payment = cash_order_with_bottles(
                db, u1, product, a1, quantity=3, status=OrderStatus.CONFIRMED
            )
            seed_unapplied_credit(db, u1, Decimal("12000.00"), admin)

            OrderService().update_order_status(
                order.id, OrderStatus.DELIVERED, updated_by=admin.id
            )
            db.session.expire_all()
            payment = Payment.query.get(payment.id)
            outcomes.append(
                {
                    "amount_collected": str(payment.amount_collected),
                    "outstanding_amount": str(payment.outstanding_amount),
                    "status": payment.status.value,
                    "prepaid_balance": str(
                        CashCollectionService().get_customer_prepaid_balance(u1.id)
                    ),
                }
            )

        assert outcomes[0] == outcomes[1], (
            "the DELIVERED money settlement depends on the place's bottle count — "
            f"{outcomes}"
        )
        # ...and it actually did something, so the equality above is not two
        # identical no-ops.
        assert outcomes[0]["amount_collected"] != "0.00"

    def test_coworkers_payment_is_untouched_by_a_delivery_at_the_shared_place(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        _order2, payment2 = delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        product = bottle_product(db, per_unit="2")
        order, _payment = cash_order_with_bottles(
            db, u1, product, a1, quantity=3, status=OrderStatus.CONFIRMED
        )

        before = money_snapshot(db)["payments"][payment2.id]
        place_before = BottleTrackingService.get_place_balance(a1.id)
        OrderService().update_order_status(order.id, OrderStatus.DELIVERED, updated_by=admin.id)

        assert BottleTrackingService.get_place_balance(a1.id) == place_before + Decimal("6.00")
        assert money_snapshot(db)["payments"][payment2.id] == before

    def test_return_of_the_whole_place_balance_does_not_change_expected_cash(self, db):
        """A driver returning EVERY empty at a shared place — including crates
        the coworker's deliveries put there — must not move the cash the driver
        is expected to collect.

        The driver card renders ``customer_bottle_balance`` and
        ``expected_cash_to_collect`` side by side from one handler; deriving one
        from the other is the failure this pins.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a2, 6, notes="coworker's empties")
        product = bottle_product(db, per_unit="2")
        order, payment = cash_order_with_bottles(
            db, u1, product, a1, quantity=2, status=OrderStatus.CONFIRMED
        )

        projection_before = StaffService.get_cod_collection_projection(order)
        assert projection_before["expected_cash_to_collect"] == 30000.0

        OrderService().update_order_status(
            order.id, OrderStatus.DELIVERED, updated_by=admin.id, bottles_returned=6
        )

        db.session.expire_all()
        order = Order.query.get(order.id)
        projection_after = StaffService.get_cod_collection_projection(order)
        assert projection_after == projection_before
        # Bottles: 6 seeded + 4 delivered - 6 returned = 4 at the place.
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("4.00")
        db.session.expire_all()
        payment = Payment.query.get(payment.id)
        assert str(payment.outstanding_amount) == "30000.00"
        assert str(payment.amount_collected) == "0.00"

    def test_bottle_only_operations_never_move_a_live_cod_projection(self, db):
        """Hold the order and its payment still; run every bottle writer the
        place exposes. ``get_cod_collection_projection`` must not budge — not
        even the reserved-prepayment arm, which lives in ``provider_data``.
        """
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u3)
        group = group_addresses(db, admin, a1, a2, a3)
        product = bottle_product(db, per_unit="1")
        order, payment = cash_order_with_bottles(
            db, u1, product, a1, quantity=2, status=OrderStatus.CONFIRMED
        )
        payment.provider_data = {"cod_prepayment_reserved_amount": "5000.00"}
        db.session.commit()

        baseline = StaffService.get_cod_collection_projection(order)
        assert baseline["cod_reserved_prepayment_amount"] == 5000.0

        service = BottleTrackingService()
        service.admin_adjust_balance(
            user_id=None, address_id=a1.id, adjustment=Decimal("9"),
            actor_user_id=admin.id, notes="+9",
        )
        db.session.expire_all()
        assert StaffService.get_cod_collection_projection(Order.query.get(order.id)) == baseline

        service.record_bottles_returned(
            user_id=u2.id, address_id=a2.id, quantity=Decimal("4"), actor_user_id=admin.id
        )
        db.session.commit()
        db.session.expire_all()
        assert StaffService.get_cod_collection_projection(Order.query.get(order.id)) == baseline

        CustomerLinkService().remove_address_from_group(
            a3.id, acting_admin_id=admin.id, reason="left", bottles_leaving=Decimal("2")
        )
        db.session.expire_all()
        assert StaffService.get_cod_collection_projection(Order.query.get(order.id)) == baseline
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("3.00")
        # ATTRIBUTION of the split, not just the surviving place: a3 left with
        # the two it took, and the group really is down to two members.
        assert BottleTrackingService.get_place_balance(a3.id) == Decimal("2.00")
        assert (
            UserAddress.query.filter_by(address_group_id=group.id).count() == 2
        )


# --------------------------------------------------------------------------- #
# 3. STANDALONE COLLECTION BY A DRIVER
# --------------------------------------------------------------------------- #


class TestStandaloneCollection:
    def test_collection_posts_no_cash_event_and_leaves_the_cash_session_alone(
        self, app, client, db
    ):
        """A driver collecting empties at a shared place tallies against their
        BOTTLE session. Their CASH session — the figure they are later held
        accountable for — must not move, and no ``cash_collection_events`` row
        may appear.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 9)
        delivered_cod_order(db, u2, address=a2, total=Decimal("15000.00"))

        driver = staff_user(db)
        cash_session = open_cash_session(db, driver, expected="45000.00")
        bottle_session = open_bottle_session(db, driver, loaded=50)
        headers = token_for(app, driver)

        before = money_snapshot(db)
        resp = client.post(
            "/api/v1/staff/bottles/collection",
            json={"customer_id": u2.id, "address_id": a2.id, "quantity": 9},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["remaining_balance"] == 0

        assert money_snapshot(db) == before
        db.session.expire_all()
        assert CashCollectionEvent.query.count() == 0
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")
        rows = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.STANDALONE_COLLECTION
        ).all()
        assert [str(r.quantity) for r in rows] == ["-9.00"]
        # The BOTTLE session absorbed it; the CASH session did not.
        assert DriverCashSession.query.get(cash_session.id).gross_cash_collected == Decimal(
            "0.00"
        )
        from business_app.models.bottle import DriverBottleSession

        assert (
            DriverBottleSession.query.get(bottle_session.id).bottles_collected_from_customers == 9
        )

    def test_driver_may_not_collect_at_a_strangers_place(self, app, client, db):
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_address(db, u3)  # u3 owns an address, but not at this place
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 5)
        driver = staff_user(db)
        bottle_session = open_bottle_session(db, driver, loaded=50)
        headers = token_for(app, driver)

        before = money_snapshot(db)
        resp = client.post(
            "/api/v1/staff/bottles/collection",
            json={"customer_id": u3.id, "address_id": a2.id, "quantity": 3},
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert error_code_of(resp) == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"

        db.session.rollback()
        assert money_snapshot(db) == before
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("5.00")
        from business_app.models.bottle import DriverBottleSession

        assert (
            DriverBottleSession.query.get(bottle_session.id).bottles_collected_from_customers == 0
        )


# --------------------------------------------------------------------------- #
# 4. ADMIN ADJUSTMENT / INITIAL BALANCE
# --------------------------------------------------------------------------- #


class TestAdminBalanceWrites:
    @pytest.mark.parametrize(
        "adjustment",
        ["5", "-5", "0", "0.01", "-0.01", "1000000", "-2.5"],
    )
    def test_admin_adjust_moves_the_place_and_freezes_money(self, db, adjustment):
        """``admin_adjust_balance`` is the write path an admin reaches when a
        place looks wrong — the single most plausible place for someone to bolt
        on 'and bill the customer for the difference'.

        Boundary values also catch a Decimal/float coercion that could round a
        money column read in the same transaction.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        seed_unapplied_credit(db, u1, Decimal("7000.00"), admin)
        seed_unapplied_credit(db, u2, Decimal("3000.00"), admin)

        place_before = BottleTrackingService.get_place_balance(a1.id)
        ledger_before = ledger_sum_for_place(a1.id)
        with MoneyFreeze(db, f"admin_adjust_balance({adjustment})"):
            BottleTrackingService().admin_adjust_balance(
                user_id=None,
                address_id=a1.id,
                adjustment=Decimal(adjustment),
                actor_user_id=admin.id,
                notes=f"boundary {adjustment}",
            )

        delta = Decimal(adjustment)
        assert BottleTrackingService.get_place_balance(a1.id) == place_before + delta
        assert ledger_sum_for_place(a1.id) == ledger_before + delta
        # Even a zero adjustment still appends an auditable row.
        rows = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT
        ).all()
        assert len(rows) == 1
        assert Decimal(str(rows[0].quantity)) == delta

    def test_set_initial_balance_on_a_shared_place_bills_nobody(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        credit_event = seed_unapplied_credit(db, u1, Decimal("9000.00"), admin)
        # Baseline AFTER setup: the engine's own auto-application may already
        # have spent part of this credit on u1's open debt at seeding time. That
        # is the engine doing its own job; what this test pins is that the
        # bottle write does not move the figure from wherever the engine left it.
        db.session.expire_all()
        unapplied_before = str(
            CashCollectionEvent.query.get(credit_event.id).unapplied_amount
        )

        low_member = min([a1, a2], key=lambda a: a.id)
        with MoneyFreeze(db, "set_initial_balance"):
            entry = BottleTrackingService().set_initial_balance(
                user_id=None, address_id=a2.id, quantity=Decimal("20"), actor_user_id=admin.id
            )

        # UPDATED: `set_initial_balance` no longer carries an idempotency key —
        # the key was scope-blind at lookup time and swallowed seeds for other
        # places; the guard was always the structural has-history check.
        assert entry.idempotency_key is None
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("20.00")
        # The audit stamp is DERIVED from the place's representative (lowest-id
        # member) address owner, not from the address the admin happened to use.
        assert entry.user_id == low_member.user_id
        db.session.expire_all()
        assert (
            str(CashCollectionEvent.query.get(credit_event.id).unapplied_amount)
            == unapplied_before
        )

    def test_initial_balance_rejection_leaves_money_and_bottles_untouched(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        _order, payment = delivered_cod_order(db, u1, address=a1, total=Decimal("40000.00"))
        payment.amount_collected = Decimal("15000.00")
        payment.outstanding_amount = Decimal("25000.00")
        db.session.commit()
        seed_place_bottles(db, admin, a1, 4, notes="a delivery already happened here")

        place_before = BottleTrackingService.get_place_balance(a1.id)
        ledger_rows_before = BottleLedger.query.count()
        balance_rows_before = BottleBalance.query.count()

        with MoneyFreeze(db, "rejected set_initial_balance"):
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService().set_initial_balance(
                    user_id=None, address_id=a2.id, quantity=Decimal("50"),
                    actor_user_id=admin.id,
                )
            assert exc.value.error_code == "BOTTLE_INITIAL_BALANCE_EXISTS"

        assert BottleTrackingService.get_place_balance(a1.id) == place_before
        assert BottleLedger.query.count() == ledger_rows_before
        assert BottleBalance.query.count() == balance_rows_before


# --------------------------------------------------------------------------- #
# 5. THE §7.1 SPLIT
# --------------------------------------------------------------------------- #


class TestBottlesLeavingSplit:
    def test_split_conserves_bottles_and_moves_zero_money(self, db):
        """§5.7 explicitly decided NOT to release reservations on ungroup. A
        later 'tidy up on ungroup' edit would silently reverse a customer's
        reserved credit — this is the pin that stops it.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 7)
        _order, payment = delivered_cod_order(db, u1, address=a1, total=Decimal("30000.00"))
        # A live reservation parked on the COWORKER's pending order — the money
        # the split must not touch belongs to somebody who is not leaving.
        _pending, pending_payment = delivered_cod_order(
            db, u2, total=Decimal("6000.00"), status=OrderStatus.CONFIRMED
        )
        seed_unapplied_credit(db, u2, Decimal("6000.00"), admin)
        db.session.expire_all()
        reserved_before = (pending_payment.provider_data or {}).get(
            "cod_prepayment_reserved_amount"
        )
        assert Decimal(str(reserved_before or 0)) > 0, "fixture did not reserve anything"

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "remove_address_from_group(bottles_leaving=2)"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=Decimal("2")
            )

        assert result["dissolved"] is False
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("2.00")
        assert bottle_totals(db) == totals_before  # nothing minted, nothing destroyed
        pair = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like("place_leave:%")
        ).all()
        assert len(pair) == 2
        assert sum((Decimal(str(p.quantity)) for p in pair), Decimal("0.00")) == Decimal("0.00")
        # The reservation is still live and untouched.
        db.session.expire_all()
        live = [a for a in CashCollectionAllocation.query.all() if a.reversed_at is None]
        assert live, "the fixture's reservation allocation vanished"
        assert str(
            (Payment.query.get(pending_payment.id).provider_data or {}).get(
                "cod_prepayment_reserved_amount"
            )
        ) == str(reserved_before)
        # The LEAVER's own delivered-COD debt is exactly where it was, too — the
        # split moved bottles out of the place and nothing else.
        assert str(Payment.query.get(payment.id).outstanding_amount) == "30000.00"

    def test_split_of_the_whole_place_balance_moves_no_money(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a2, 4)
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))

        cod_before = CashCollectionService().get_place_cod_statement(group.id)
        assert cod_before["total_outstanding_amount"] == 35000.0
        totals_before = bottle_totals(db)

        with MoneyFreeze(db, "split the whole place balance"):
            CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=Decimal("4")
            )

        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("0.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("4.00")
        assert bottle_totals(db) == totals_before
        # The place statement is a LIVE-membership read: a1's own delivered-COD
        # order leaves the statement because a1 is no longer a member, and the
        # debt itself is untouched (MoneyFreeze above).
        cod_after = CashCollectionService().get_place_cod_statement(group.id)
        assert cod_after["total_outstanding_amount"] == 20000.0
        assert len(cod_after["items"]) == 1
        # a2 and a3 are both u2's, so the remaining place has ONE distinct owner.
        assert cod_after["member_count"] == 1

    @pytest.mark.parametrize(
        "bad",
        [4, -1, Decimal("NaN"), float("inf"), float("nan"), "abc", "1e400"],
    )
    def test_rejected_split_writes_nothing_at_all(self, db, bad):
        """The event row is flushed BEFORE the split in the success path, and
        the validation is deliberately placed before it. A reordering would
        leave a flushed ``CustomerLinkEvent`` for the NEXT commit on this
        session to adopt — and that next commit may be a money write.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 3)
        _order, payment = delivered_cod_order(db, u1, address=a1, total=Decimal("30000.00"))
        payment.amount_collected = Decimal("10000.00")
        payment.outstanding_amount = Decimal("20000.00")
        db.session.commit()

        ledger_before = BottleLedger.query.count()
        events_before = CustomerLinkEvent.query.count()

        with MoneyFreeze(db, f"rejected split bottles_leaving={bad!r}"):
            with pytest.raises(ValidationError) as exc:
                CustomerLinkService().remove_address_from_group(
                    a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=bad
                )
            assert exc.value.error_code == "PLACE_SPLIT_INVALID"

        db.session.expire_all()
        assert UserAddress.query.get(a1.id).address_group_id is not None
        assert BottleLedger.query.count() == ledger_before
        assert CustomerLinkEvent.query.count() == events_before
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("3.00")

    def test_none_is_the_legal_default_and_is_not_rejected(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 3)

        with MoneyFreeze(db, "removal with bottles_leaving=None"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=None
            )
        assert result["bottles_leaving"] == Decimal("0.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("3.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")

    def test_split_on_a_negative_place_rejects_anything_non_zero(self, db):
        """``max(0, place)`` carries three spec arms in one expression. A clamp
        instead of a rejection would silently transfer a NEGATIVE quantity,
        which the customer bottle screen renders as a debt the customer never
        incurred — and the natural 'fix' for that is a fine, which is
        money-shaped.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a2, -3, notes="a coworker over-returned")
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("-3.00")

        with MoneyFreeze(db, "non-zero split on a negative place"):
            with pytest.raises(ValidationError) as exc:
                CustomerLinkService().remove_address_from_group(
                    a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=1
                )
            assert exc.value.error_code == "PLACE_SPLIT_INVALID"

        with MoneyFreeze(db, "zero split on a negative place"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=0
            )
        assert result["bottles_leaving"] == Decimal("0.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("-3.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")


# --------------------------------------------------------------------------- #
# 6. §7.3 DISSOLVE
# --------------------------------------------------------------------------- #


def legacy_drifted_address(db, admin, user, quantity):
    """An address carrying a stored balance its ledger cannot explain.

    This is dev address 24's shape (stored 20.00, ZERO ledger rows) and the one
    that breaks on grouping. The balance ROW is produced by the real writer
    (``admin_adjust_balance``); only the ledger rows are then removed, standing
    in for a figure seeded before the ledger existed. Nothing is hand-built.
    """
    address = make_address(db, user)
    BottleTrackingService().admin_adjust_balance(
        user_id=None,
        address_id=address.id,
        adjustment=Decimal(str(quantity)),
        actor_user_id=admin.id,
        notes="legacy hand adjustment",
    )
    BottleLedger.query.filter(BottleLedger.address_id == address.id).delete(
        synchronize_session=False
    )
    db.session.commit()
    db.session.expire_all()
    assert BottleTrackingService.get_place_balance(address.id) == Decimal(str(quantity))
    assert ledger_sum_for_place(address.id) == Decimal("0.00")
    return address


class TestDissolve:
    def test_dissolve_of_a_two_member_place_conserves_bottles_and_freezes_money(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a2, 5, notes="a2's own history")
        seed_place_bottles(db, admin, a1, 2, notes="a1's own history")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "dissolve of a two-member place"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left"
            )

        assert result["dissolved"] is True
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("7.00")
        assert bottle_totals(db) == totals_before
        # The group's balance row is GONE; the AddressGroup row is KEPT (the
        # ledger FK makes deleting it impossible).
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        assert AddressGroup.query.get(group.id) is not None
        pair = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"place_dissolve:{group.id}:%")
        ).all()
        assert len(pair) == 2
        assert sum((Decimal(str(p.quantity)) for p in pair), Decimal("0.00")) == Decimal("0.00")
        report = reconcile_customer_link_invariants()
        assert report["orphaned_place_balances"] == []
        assert report["stranded_address_balances"] == []

    def test_dissolve_of_a_place_its_ledger_cannot_explain_loses_no_bottles(self, db):
        """The address-24 shape. A lazy 'just call reconcile_balance' here would
        zero twenty real bottles — and 'compensating' for that loss with a fine
        or a credit is the boundary crossing in its most damaging direction.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1 = legacy_drifted_address(db, admin, u1, 20)
        a2 = make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("20.00")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "dissolve of a drifted place"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left"
            )

        assert result["dissolved"] is True
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("20.00")
        # BOTH sides of the pair. The dissolve of a drifted place is exactly the
        # shape where a one-sided check is blind: the carry is balance-only, the
        # remainder crosses as a paired ledger append, and a sign slip on either
        # leg conserves the STORED total while corrupting the ledger.
        assert bottle_totals(db) == totals_before
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        # ATTRIBUTION, which the global pair above cannot see: the +20 leg lands
        # in the SURVIVOR's own scope (so its stored figure is finally ledger-
        # explained) and the -20 leg stays parked on the inert group. A swap of
        # the two legs conserves both global totals and still loses the bottles.
        assert ledger_sum_for_place(a2.id) == Decimal("20.00")
        assert ledger_sum_for_group(group.id) == Decimal("-20.00")
        assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []

    def test_dissolve_with_zero_members_remaining(self, db):
        """A one-member place emptied — the arm where ``survivor_id`` falls back
        to the DEPARTING address, and the reason
        ``release_group_history_to_address`` passes ``allow_memberless=True``.

        UPDATED: this used to be reached by repopulating a dissolved group to
        exactly ONE member, which was possible only because
        ``add_addresses_to_group`` had no minimum-member check. It now REFUSES a
        memberless group by name (``PLACE_GROUP_DISSOLVED``), so no service path
        builds a one-member place at all. The state still exists in data written
        before that refusal, so the membership is pointed by hand rather than the
        arm being left untested — an untested arm is how it would quietly rot,
        and without ``allow_memberless`` this dissolve 500s.
        """
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 6)
        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="dissolve it"
        )
        # Repopulate the (memberless, still-existing) group with ONE address.
        a9 = make_address(db, u3)
        seed_place_bottles(db, admin, a9, 3, notes="a9's own bottles")
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().add_addresses_to_group(
                group.id, [a9.id], acting_admin_id=admin.id, reason="repopulate"
            )
        assert exc.value.error_code == "PLACE_GROUP_DISSOLVED"
        db.session.rollback()
        _force_one_member_place(db, a9.id, group.id)
        delivered_cod_order(db, u3, address=a9, total=Decimal("25000.00"))
        db.session.expire_all()
        place_before = BottleTrackingService.get_place_balance(a9.id)
        totals_before = bottle_totals(db)

        with MoneyFreeze(db, "removal of the only member"):
            result = CustomerLinkService().remove_address_from_group(
                a9.id, acting_admin_id=admin.id, reason="last one out"
            )

        assert result["dissolved"] is True
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a9.id) == place_before
        assert place_before == Decimal("3.00")  # a9's own three, and nothing else
        # BOTH sides of the pair — the zero-remaining arm re-stamps the departing
        # address's own entries AND releases the remainder to it, and a
        # double-count there mints on the ledger side alone.
        assert bottle_totals(db) == totals_before
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        assert CashCollectionService().get_place_cod_statement(group.id)[
            "total_outstanding_amount"
        ] == 0.0
        assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []

    def test_split_and_dissolve_in_one_call_conserve_the_pair(self, db):
        """Two conserving mechanisms run back to back on the same two balance
        rows inside ONE transaction, with an ``expire_all`` between them.
        Double-counting the split inside the dissolve's ``inherited`` is the
        exact arithmetic mistake that MINTS bottles — and a minted-bottle report
        is what triggers a fine.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 2, notes="a1's own")
        seed_place_bottles(db, admin, a2, 4, notes="a2's own")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "split + dissolve in one call"):
            result = CustomerLinkService().remove_address_from_group(
                a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=Decimal("2")
            )

        assert result["dissolved"] is True
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("2.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("4.00")
        assert bottle_totals(db) == totals_before
        assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []

    def test_dissolved_place_group_detail_is_still_readable_by_an_admin(
        self, app, client, db, admin_auth_headers
    ):
        """``GET /admin/place-groups/<id>`` calls ``get_place_cod_statement`` on a
        group that still EXISTS but has no members, right after the dissolve
        deleted its balance row. If that raises ``NotFoundError`` the route's
        ``except NotFoundError`` turns the dissolved-group view into a 404 and
        the admin can never read the audit trail of the dissolve they just
        performed.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 5)
        _o1, p1 = delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        _o2, p2 = delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))

        before = money_snapshot(db)
        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="dissolve"
        )
        assert money_snapshot(db) == before

        resp = client.get(f"/api/v1/admin/place-groups/{group.id}", headers=admin_auth_headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["place_balance"] == 0
        assert data["members"] == []
        assert data["cod"]["total_outstanding_amount"] == 0.0
        assert data["cod"]["member_count"] == 0
        # The audit trail of the dissolve is still readable.
        assert "remove_from_place_group" in {e["event_type"] for e in data["events"]}
        # The two underlying debts are exactly where they were.
        db.session.expire_all()
        assert str(Payment.query.get(p1.id).outstanding_amount) == "15000.00"
        assert str(Payment.query.get(p2.id).outstanding_amount) == "20000.00"


# --------------------------------------------------------------------------- #
# 7. §7.2 JOIN AND §7.4 MERGE REVIEW
# --------------------------------------------------------------------------- #


class TestJoinAndMergeReview:
    def test_join_absorbs_both_balances_and_moves_no_money(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_place_bottles(db, admin, a1, 4)
        seed_place_bottles(db, admin, a2, 3)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        seed_unapplied_credit(db, u1, Decimal("2000.00"), admin)
        seed_unapplied_credit(db, u2, Decimal("2000.00"), admin)

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "create_place_group"):
            group = group_addresses(db, admin, a1, a2, reason="office")

        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("7.00")
        assert bottle_totals(db) == totals_before
        # Both address-keyed rows are gone; there is exactly ONE row for the place.
        assert BottleBalance.query.filter(BottleBalance.address_id.isnot(None)).count() == 0
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 1
        # Running snapshots walk the merged timeline in (occurred_at, id) order.
        rows = (
            BottleLedger.query.filter(BottleLedger.address_group_id == group.id)
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc())
            .all()
        )
        running = Decimal("0.00")
        for row in rows:
            running += Decimal(str(row.quantity))
            assert Decimal(str(row.balance_after)) == running
        assert running == Decimal("7.00")
        assert reconcile_customer_link_invariants()["stranded_address_balances"] == []

    def test_join_does_not_pool_cluster_prepaid_credit(self, db):
        """Place scope and cluster scope are different keys onto the same money
        engine. If grouping triggered the auto-reservation sweep or resolved an
        allocation scope as 'place', one stranger's prepaid money would settle
        another stranger's debt with NO admin decision.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_unapplied_credit(db, u1, Decimal("50000.00"), admin)
        _order2, payment2 = delivered_cod_order(db, u2, address=a2, total=Decimal("50000.00"))
        service = CashCollectionService()
        assert service.get_customer_prepaid_balance(u1.id) == Decimal("50000.00")

        allocations_before = CashCollectionAllocation.query.count()
        with MoneyFreeze(db, "join of two strangers' addresses"):
            group = group_addresses(db, admin, a1, a2)

        db.session.expire_all()
        assert service.get_customer_prepaid_balance(u1.id) == Decimal("50000.00")
        assert service.get_customer_prepaid_balance(u2.id) == Decimal("0.00")
        assert str(Payment.query.get(payment2.id).outstanding_amount) == "50000.00"
        assert CashCollectionAllocation.query.count() == allocations_before
        # The place-keyed READ does change — that is membership, and only that.
        assert service.get_place_cod_statement(group.id)["total_outstanding_amount"] == 50000.0

    def test_merge_exclusion_changes_bottles_only(self, db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_place_bottles(db, admin, a1, 6, notes="real")
        bogus = seed_place_bottles(db, admin, a1, 4, notes="bogus entry")
        seed_place_bottles(db, admin, a2, 3, notes="real")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "merge review with an exclusion"):
            group = CustomerLinkService().create_place_group(
                [a1.id, a2.id],
                acting_admin_id=admin.id,
                reason="that entry was wrong",
                excluded_ledger_entry_ids=[bogus.id],
            )

        db.session.expire_all()
        # An exclusion is a COUPLED append, so it moves the stored figure and the
        # ledger sum by the SAME -4. Asserting only the balance would not tell a
        # coupled reversal from a decoupled one, and a decoupled reversal here
        # would leave the place's two figures permanently four apart.
        assert bottle_totals(db) == (
            totals_before[0] - Decimal("4"),
            totals_before[1] - Decimal("4"),
        )
        assert ledger_sum_for_group(group.id) == Decimal("9.00")
        # History is never rewritten: the bogus row is still there, and a
        # REVERSING entry was appended next to it.
        assert BottleLedger.query.get(bogus.id) is not None
        reversal = BottleLedger.query.filter(
            BottleLedger.idempotency_key.like(f"merge_exclude:{group.id}:%")
        ).one()
        assert Decimal(str(reversal.quantity)) == Decimal("-4.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("9.00")

    @pytest.mark.parametrize("stated", ["20", "5"])
    def test_resulting_balance_override_changes_bottles_only(self, db, stated):
        """The override is the ONLY sanctioned way the bottle total may change,
        and the only place in the feature where a number an admin TYPES becomes
        a persisted quantity — the exact shape of a money field.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_place_bottles(db, admin, a1, 7)
        seed_place_bottles(db, admin, a2, 5)
        _order, payment = delivered_cod_order(db, u1, address=a1, total=Decimal("40000.00"))
        payment.amount_collected = Decimal("15000.00")
        payment.outstanding_amount = Decimal("25000.00")
        payment.collected_by = admin.id
        payment.paid_at = datetime.now(UTC)
        db.session.commit()
        payment_before = money_snapshot(db)["payments"][payment.id]

        with MoneyFreeze(db, f"merge override to {stated}"):
            group = CustomerLinkService().create_place_group(
                [a1.id, a2.id],
                acting_admin_id=admin.id,
                reason="counted the crates",
                resulting_balance=Decimal(stated),
            )

        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal(stated)
        # THE strongest guard on the feature: after a reviewed merge the stored
        # figure and the ledger sum AGREE.
        assert ledger_sum_for_group(group.id) == Decimal(stated)
        correction = BottleLedger.query.filter_by(
            idempotency_key=f"merge_correction:{group.id}:"
            f"{CustomerLinkEvent.query.order_by(CustomerLinkEvent.id.desc()).first().id}"
        ).one()
        assert Decimal(str(correction.quantity)) == Decimal(stated) - Decimal("12")
        assert money_snapshot(db)["payments"][payment.id] == payment_before

    def test_merge_backfill_is_the_only_balance_decoupled_write(self, db):
        """The address-24 shape joined and reviewed. A COUPLED backfill would
        mint the drift a second time; a decoupled write leaking into another
        namespace would break the conservation split entirely.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a_x = legacy_drifted_address(db, admin, u1, 20)
        a_y = make_address(db, u2)
        seed_place_bottles(db, admin, a_y, 0, notes="clean, no drift")
        delivered_cod_order(db, u1, address=a_x, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a_y, total=Decimal("20000.00"))

        stored_before = bottle_totals(db)[0]
        with MoneyFreeze(db, "merge review with a backfill"):
            group = CustomerLinkService().create_place_group(
                [a_x.id, a_y.id],
                acting_admin_id=admin.id,
                reason="counted",
                resulting_balance=Decimal("20"),
            )

        db.session.expire_all()
        event_id = (
            CustomerLinkEvent.query.filter_by(event_type="create_place_group")
            .order_by(CustomerLinkEvent.id.desc())
            .first()
            .id
        )
        backfill = BottleLedger.query.filter_by(
            idempotency_key=f"merge_backfill:{group.id}:{event_id}"
        ).one()
        assert Decimal(str(backfill.quantity)) == Decimal("20.00")
        # The DECOUPLED write moved the ledger only: the stored total is exactly
        # what it was (the join carried it, the backfill did not add to it).
        assert bottle_totals(db)[0] == stored_before
        assert BottleTrackingService.get_place_balance(a_x.id) == Decimal("20.00")
        assert ledger_sum_for_group(group.id) == Decimal("20.00")

    @pytest.mark.parametrize(
        "key", [None, "", "delivery:5", "merge_correction:1:2", "merge_backfillX:1:2"]
    )
    def test_the_decoupled_writer_refuses_any_key_outside_its_namespace(self, db, key):
        """'merge_backfillX' passes a naive ``startswith`` without the colon; the
        empty string passes a naive ``is not None``. If an unkeyed decoupled row
        ever lands, the conservation pin counts it as COUPLED and the two
        figures diverge under a green suite.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 5)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))

        ledger_before = BottleLedger.query.count()
        balances_before = bottle_totals(db)
        with MoneyFreeze(db, f"decoupled write with key {key!r}"):
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService()._create_ledger_backfill_entry(
                    scope=BottleScope.for_group(group.id),
                    user_id=u1.id,
                    address_id=a1.id,
                    quantity=Decimal("3"),
                    actor_user_id=admin.id,
                    idempotency_key=key,
                )
            assert exc.value.error_code == "BOTTLE_DECOUPLED_KEY_REQUIRED"

        assert BottleLedger.query.count() == ledger_before
        assert bottle_totals(db) == balances_before

    def test_stale_preview_writes_nothing_at_all(self, db):
        """The guards sit BEFORE any write precisely so a rejected review leaves
        nothing flushed for the next commit on this session. If a guard moved
        after the ``AddressGroup`` flush, a subsequent money commit in the same
        request would persist an orphan group — and the place-keyed COD reader
        would then unify two strangers' debts.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_place_bottles(db, admin, a1, 4)
        seed_place_bottles(db, admin, a2, 3)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        preview = BottleTrackingService.build_merge_preview([a1.id, a2.id])
        stale_ids = list(preview["entry_ids"])

        # A real delivery lands at a1 before the join is submitted.
        seed_place_bottles(db, admin, a1, 2, notes="late arrival")

        groups_before = AddressGroup.query.count()
        ledger_before = BottleLedger.query.count()
        with MoneyFreeze(db, "stale merge preview"):
            with pytest.raises(ValidationError) as exc:
                CustomerLinkService().create_place_group(
                    [a1.id, a2.id],
                    acting_admin_id=admin.id,
                    reason="counted",
                    resulting_balance=Decimal("10"),
                    preview_entry_ids=stale_ids,
                )
            assert exc.value.error_code == "MERGE_PREVIEW_STALE"

        db.session.expire_all()
        assert AddressGroup.query.count() == groups_before
        assert BottleLedger.query.count() == ledger_before
        assert UserAddress.query.get(a1.id).address_group_id is None
        assert UserAddress.query.get(a2.id).address_group_id is None
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("6.00")
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("3.00")

    def test_merge_guard_codes_all_leave_money_frozen(self, db):
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u3)
        entry1 = seed_place_bottles(db, admin, a1, 4)
        seed_place_bottles(db, admin, a2, 3)
        stray = seed_place_bottles(db, admin, a3, 9, notes="an unrelated third address")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        service = CustomerLinkService()

        # (a) an exclusion with a blank reason
        with MoneyFreeze(db, "MERGE_REASON_REQUIRED"):
            with pytest.raises(ValidationError) as exc:
                service.create_place_group(
                    [a1.id, a2.id], acting_admin_id=admin.id, reason="  ",
                    excluded_ledger_entry_ids=[entry1.id],
                )
            assert exc.value.error_code == "MERGE_REASON_REQUIRED"

        # (b) an exclusion naming an entry from a THIRD, unrelated address
        with MoneyFreeze(db, "MERGE_EXCLUSION_NOT_ELIGIBLE (stray id)"):
            with pytest.raises(ValidationError) as exc:
                service.create_place_group(
                    [a1.id, a2.id], acting_admin_id=admin.id, reason="why",
                    excluded_ledger_entry_ids=[stray.id],
                )
            assert exc.value.error_code == "MERGE_EXCLUSION_NOT_ELIGIBLE"

        # (c) a non-integer exclusion id is a plain 400 on the message alone
        with MoneyFreeze(db, "garbage exclusion id"):
            with pytest.raises(ValidationError) as exc:
                service.create_place_group(
                    [a1.id, a2.id], acting_admin_id=admin.id, reason="why",
                    excluded_ledger_entry_ids=["not-an-id"],
                )
            assert exc.value.error_code is None

        assert AddressGroup.query.count() == 0
        assert UserAddress.query.get(a1.id).address_group_id is None

    def test_double_exclusion_across_two_episodes_is_rejected(self, db):
        """The idempotency key is EPISODE-scoped, so a re-join would happily
        write a SECOND reversal and destroy the bottles twice.
        """
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        # A THIRD member so the removal below does not DISSOLVE the place: a
        # dissolved group is now refused as a join target by name
        # (PLACE_GROUP_DISSOLVED), which would mask the guard under test.
        a3 = make_address(db, u3)
        bogus = seed_place_bottles(db, admin, a1, 4, notes="bogus")
        seed_place_bottles(db, admin, a2, 3)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        service = CustomerLinkService()
        group = service.create_place_group(
            [a1.id, a2.id, a3.id], acting_admin_id=admin.id, reason="first episode",
            excluded_ledger_entry_ids=[bogus.id],
        )
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("3.00")

        service.remove_address_from_group(a1.id, acting_admin_id=admin.id, reason="left")
        db.session.expire_all()
        place_after_removal = BottleTrackingService.get_place_balance(a2.id)

        with MoneyFreeze(db, "re-exclusion in a second episode"):
            with pytest.raises(ValidationError) as exc:
                service.add_addresses_to_group(
                    group.id, [a1.id], acting_admin_id=admin.id, reason="second episode",
                    excluded_ledger_entry_ids=[bogus.id],
                )
            assert exc.value.error_code == "MERGE_EXCLUSION_NOT_ELIGIBLE"

        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a2.id) == place_after_removal

    def test_oversized_merge_cannot_be_corrected_even_bypassing_the_route(self, db):
        """The cap lives in TWO places on purpose. If only the route carried it,
        a client that never rendered the preview could still post an override
        for a place it could not have counted — an unfounded quantity change
        with no reviewer.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        cap = BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES
        service = BottleTrackingService()
        for i in range(cap + 1):
            service.admin_adjust_balance(
                user_id=None, address_id=a1.id, adjustment=Decimal("0"),
                actor_user_id=admin.id, notes=f"bulk {i}",
            )
        assert BottleLedger.query.filter_by(address_id=a1.id).count() == cap + 1

        with MoneyFreeze(db, "oversized merge with an override"):
            with pytest.raises(ValidationError) as exc:
                CustomerLinkService().create_place_group(
                    [a1.id, a2.id], acting_admin_id=admin.id, reason="counted",
                    resulting_balance=Decimal("3"),
                )
            assert exc.value.error_code is None
            assert str(cap) in str(exc.value)

        assert AddressGroup.query.count() == 0

        # A PLAIN join of the same addresses is untouched by the cap.
        with MoneyFreeze(db, "plain join of an oversized merge"):
            group = CustomerLinkService().create_place_group(
                [a1.id, a2.id], acting_admin_id=admin.id, reason="plain"
            )
        assert group is not None
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")


# --------------------------------------------------------------------------- #
# 8. reconcile_balance — destructive on bottles, inert on money
# --------------------------------------------------------------------------- #


class TestReconcileBalance:
    def test_reconcile_destroys_carried_bottles_and_touches_no_money(
        self, app, client, db, admin_auth_headers
    ):
        """``POST /admin/bottles/reconcile/<address_id>`` assigns
        ``balance = ledger_sum`` unconditionally, writes NO ledger entry and
        creates NO audit row. On a place whose figure is CARRIED rather than
        ledger-derived it destroys real bottles with no trace.

        Pinned as CURRENT behaviour (this is by construction, not a defect in
        this branch's code) — what the test guarantees is that the destruction
        stays confined to bottles and never grows a money leg.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a_x = legacy_drifted_address(db, admin, u1, 20)
        a_y = make_address(db, u2)
        group = group_addresses(db, admin, a_x, a_y)
        _order, payment = delivered_cod_order(db, u1, address=a_x, total=Decimal("30000.00"))
        seed_unapplied_credit(db, u1, Decimal("4000.00"), admin)
        assert BottleTrackingService.get_place_balance(a_x.id) == Decimal("20.00")

        from business_app.models.audit import AuditLog

        audits_before = AuditLog.query.count()
        before = money_snapshot(db)
        resp = client.post(
            f"/api/v1/admin/bottles/reconcile/{a_x.id}", headers=admin_auth_headers
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert Decimal(str(data["discrepancy"])) == Decimal("20.00")
        assert data["corrected"] is True

        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a_x.id) == Decimal("0.00")
        # No ledger entry recorded the loss, and no audit row either.
        assert ledger_sum_for_group(group.id) == Decimal("0.00")
        assert AuditLog.query.count() == audits_before
        # ...and not one money value moved.
        assert money_snapshot(db) == before
        assert (
            str(Payment.query.get(payment.id).outstanding_amount)
            == before["payments"][payment.id]["outstanding_amount"]
        )

    def test_reconcile_after_a_reviewed_merge_is_a_no_op(
        self, app, client, db, admin_auth_headers
    ):
        """This is the whole point of the backfill. If its sign flipped,
        reconcile would drive the balance to the NEGATIVE of the admin's stated
        number — and 'you owe us N bottles' is the input to a fine.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a_x = legacy_drifted_address(db, admin, u1, 20)
        a_y = make_address(db, u2)
        group = CustomerLinkService().create_place_group(
            [a_x.id, a_y.id], acting_admin_id=admin.id, reason="counted",
            resulting_balance=Decimal("20"),
        )
        delivered_cod_order(db, u1, address=a_x, total=Decimal("30000.00"))
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a_x.id) == ledger_sum_for_group(group.id)

        before = money_snapshot(db)
        resp = client.post(
            f"/api/v1/admin/bottles/reconcile/{a_x.id}", headers=admin_auth_headers
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert Decimal(str(data["discrepancy"])) == Decimal("0.00")
        assert data["corrected"] is False
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(a_x.id) == Decimal("20.00")
        assert money_snapshot(db) == before


# --------------------------------------------------------------------------- #
# 9. FINES — the one bottle object denominated in UZS
# --------------------------------------------------------------------------- #


def issue_fine_via_route(client, headers, *, address_id, quantity, amount, user_id=None):
    payload = {"address_id": address_id, "quantity": quantity, "fine_amount": amount}
    if user_id is not None:
        payload["user_id"] = user_id
    return client.post("/api/v1/admin/bottles/fines", json=payload, headers=headers)


class TestFinesNeverBecomeMoney:
    """``fine_amount`` is denominated in UZS. It is the single most plausible
    thing for someone to 'wire up properly' into the receivables system — and
    doing so would put a bottle dispute into the COD cap that blocks a customer
    from ordering at all.
    """

    def test_issuing_a_fine_creates_no_payment_no_event_and_no_order(
        self, app, client, db, admin_auth_headers
    ):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 2)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))

        low_member = min([a1, a2], key=lambda a: a.id)
        payments_before = Payment.query.count()
        orders_before = Order.query.count()
        before = money_snapshot(db)

        resp = issue_fine_via_route(
            client, admin_auth_headers, address_id=a2.id, quantity=6, amount=300000
        )
        assert resp.status_code == 200, resp.get_json()

        db.session.expire_all()
        fine = BottleFine.query.one()
        assert fine.status == BottleFineStatus.PENDING
        assert fine.address_group_id == group.id  # frozen to the PLACE
        # user_id was omitted, so the stamp is DERIVED from the place's
        # representative (lowest-id member) address owner.
        assert fine.user_id == low_member.user_id
        assert Decimal(str(fine.fine_amount)) == Decimal("300000.00")

        issued = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_ISSUED
        ).one()
        assert Decimal(str(issued.quantity)) == Decimal("0.00")
        assert issued.entry_metadata["place_balance_at_issue"] == 2.0
        assert issued.address_group_id == group.id

        # Not one receivable was created anywhere.
        assert Payment.query.count() == payments_before
        assert Order.query.count() == orders_before
        assert CashCollectionEvent.query.count() == 0
        assert money_snapshot(db) == before
        # The bottles themselves did not move: a fine is a claim, not a return.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("2.00")

    def test_a_fine_never_appears_in_any_cod_surface(self, app, client, db):
        """``get_place_cod_statement`` and 'Bottles at this place' are rendered
        in the SAME admin panel. A join added to unify the two views would put a
        fine into the cap arithmetic and lock a customer out of COD over an
        unreturned crate.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 2)
        delivered_cod_order(db, u2, address=a2, total=Decimal("15000.00"))

        service = CashCollectionService()
        BottleTrackingService().issue_fine(
            user_id=None, address_id=a2.id, quantity=Decimal("6"),
            fine_amount=Decimal("300000"), actor_user_id=admin.id,
        )
        db.session.commit()
        db.session.expire_all()

        place = service.get_place_cod_statement(group.id)
        assert place["total_outstanding_amount"] == 15000.0
        assert place["active_cod_debt_count"] == 1
        assert "300000" not in repr(place)

        own = service.get_customer_cod_statement(u2.id)
        assert own["total_outstanding_amount"] == 15000.0
        assert "300000" not in repr(own)

        context = service.get_place_cod_context(a2.id)
        assert context["place_outstanding_cod_total"] == 15000.0
        assert context["place_active_cod_debt_count"] == 1

        restriction = service.get_cod_restriction_context(u2.id, delivery_address_id=a2.id)
        assert restriction["cod_restricted"] is False
        assert restriction["place_active_cod_debt_count"] == 1
        assert restriction["active_cod_debt_count"] == 1

        headers = token_for(app, u2)
        resp = client.get("/api/v1/payments/my-cod-summary", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert "300000" not in resp.get_data(as_text=True)

    def test_mark_fine_paid_moves_bottles_and_not_one_money_row(
        self, app, client, db, admin_auth_headers
    ):
        """'Paid' is a money word on a non-money object. The cash the customer
        actually handed over is invisible to the cash system BY DESIGN — pinning
        that keeps someone from 'fixing' it by minting a collection event the
        driver is then held accountable for.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 8)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        driver = staff_user(db)
        cash_session = open_cash_session(db, driver, expected="45000.00")
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a2.id, quantity=Decimal("6"),
            fine_amount=Decimal("300000"), actor_user_id=admin.id,
        )
        db.session.commit()

        before = money_snapshot(db)
        resp = client.put(
            f"/api/v1/admin/bottles/fines/{fine.id}",
            json={"action": "mark_paid"},
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()

        db.session.expire_all()
        fine = BottleFine.query.get(fine.id)
        assert fine.status == BottleFineStatus.PAID
        assert fine.paid_at is not None
        paid = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID).one()
        assert Decimal(str(paid.quantity)) == Decimal("-6.00")
        assert paid.address_group_id == group.id  # the FROZEN scope
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("2.00")
        # Money: nothing at all.
        assert money_snapshot(db) == before
        assert CashCollectionEvent.query.count() == 0
        assert DriverCashSession.query.get(cash_session.id).expected_cash == Decimal("45000.00")

    def test_paying_a_fine_frozen_to_a_group_debits_the_PLACE_not_the_leaver(self, db):
        """The freeze exists so FINE_ISSUED and FINE_PAID cannot straddle two
        ledgers. If ``_fine_scope`` ever re-resolved from the address, the pair
        would split and BOTH balances would be wrong — and a wrong balance is
        what generates the next fine.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a2, 8)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        service = BottleTrackingService()
        fine = service.issue_fine(
            user_id=u1.id, address_id=a1.id, quantity=Decimal("3"),
            fine_amount=Decimal("90000"), actor_user_id=admin.id,
        )
        db.session.commit()

        # a1 leaves with no bottles; the place is still standing (a2 + a3).
        CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left", bottles_leaving=0
        )
        db.session.expire_all()
        assert UserAddress.query.get(a1.id).address_group_id is None

        totals_before = bottle_totals(db)
        with MoneyFreeze(db, "paying a frozen-scope fine after the member left"):
            service.mark_fine_paid(fine.id, actor_user_id=admin.id)
            db.session.commit()

        db.session.expire_all()
        # The PLACE lost the bottles; a1's own fresh scope stays at 0.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")
        paid = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID).one()
        assert paid.address_group_id == group.id
        # Three bottles genuinely left the world here (a settled fine accounts
        # for them), so the pair moves by exactly -3 on BOTH sides.
        assert bottle_totals(db) == (
            totals_before[0] - Decimal("3"),
            totals_before[1] - Decimal("3"),
        )

    def test_double_paying_or_paying_a_waived_fine_is_a_conflict_that_moves_nothing(self, db):
        """Repeated admin clicks are routine. A second FINE_PAID would destroy
        the fine quantity twice, driving the place negative — which the customer
        bot then renders as a credit.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 10)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        service = BottleTrackingService()
        paid_fine = service.issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("2"),
            fine_amount=Decimal("50000"), actor_user_id=admin.id,
        )
        waived_fine = service.issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("3"),
            fine_amount=Decimal("70000"), actor_user_id=admin.id,
        )
        db.session.commit()
        service.mark_fine_paid(paid_fine.id, actor_user_id=admin.id)
        service.waive_fine(waived_fine.id, actor_user_id=admin.id)
        db.session.commit()
        db.session.expire_all()

        place_after = BottleTrackingService.get_place_balance(a1.id)
        assert place_after == Decimal("8.00")
        totals_before = bottle_totals(db)

        for fine_id, action in (
            (paid_fine.id, "pay"),
            (paid_fine.id, "waive"),
            (waived_fine.id, "pay"),
            (waived_fine.id, "waive"),
        ):
            with MoneyFreeze(db, f"{action} on an already-settled fine"):
                with pytest.raises(ConflictError):
                    if action == "pay":
                        service.mark_fine_paid(fine_id, actor_user_id=admin.id)
                    else:
                        service.waive_fine(fine_id, actor_user_id=admin.id)

        db.session.expire_all()
        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_PAID
        ).count() == 1
        assert BottleTrackingService.get_place_balance(a1.id) == place_after
        assert bottle_totals(db) == totals_before

    def test_a_driver_may_not_fine_a_strangers_place(self, app, client, db):
        """The scope check replaced a ``balance.user_id`` check the re-key made
        impossible. It is now the ONLY thing stopping a fine — a money-
        denominated obligation — being attached to a person with nothing to do
        with the place.
        """
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        make_address(db, u3)
        group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 4)
        driver = staff_user(db)
        headers = token_for(app, driver)

        before = money_snapshot(db)
        resp = client.post(
            "/api/v1/staff/bottles/fine",
            json={
                "customer_id": u3.id,
                "address_id": a2.id,
                "quantity": 3,
                "fine_amount": 90000,
            },
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert error_code_of(resp) == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"

        db.session.rollback()
        assert BottleFine.query.count() == 0
        assert money_snapshot(db) == before

    @pytest.mark.parametrize(
        "payload_patch,expected_word",
        [
            ({"quantity": 0}, "quantity"),
            ({"fine_amount": 0}, "amount"),
        ],
    )
    def test_staff_fine_route_rejects_zero_with_the_RIGHT_reason(
        self, app, client, db, payload_patch, expected_word
    ):
        """FIXED — the strict xfail is gone.

        WAS: `create_bottle_fine_staff` guarded with
        `if not all([customer_id, address_id, quantity, fine_amount])`, so a
        quantity of 0 or a fine_amount of 0 was FALSY and the driver was told
        'customer_id, address_id, quantity, and fine_amount are required'
        instead of the service's 'must be positive' — and a client retrying with
        a nonzero placeholder issues a REAL money-denominated fine.

        NOW the guard tests PRESENCE (`value is None`), so the zero reaches
        `issue_fine` and is refused for the reason it is actually wrong. The
        admin route (`BottleFineCreateRequest`) never shared the bug; the two
        entry points now reject the same input for the same reason.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        driver = staff_user(db)
        headers = token_for(app, driver)

        before = money_snapshot(db)
        payload = {
            "customer_id": u2.id,
            "address_id": a2.id,
            "quantity": 5,
            "fine_amount": 90000,
        }
        payload.update(payload_patch)
        resp = client.post("/api/v1/staff/bottles/fine", json=payload, headers=headers)

        assert resp.status_code == 400, resp.get_json()
        message = (resp.get_json() or {}).get("message") or ""
        assert "must be positive" in message.lower(), message
        assert expected_word in message.lower(), message
        db.session.rollback()
        assert BottleFine.query.count() == 0
        assert money_snapshot(db) == before

    def test_staff_fine_route_rejects_a_negative_quantity_with_the_right_reason(
        self, app, client, db
    ):
        """The control for the case above: ``-1`` is TRUTHY, so it reaches the
        service and the driver gets the correct message. The contrast is what
        makes the zero case a bug rather than a design choice.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group_addresses(db, admin, a1, a2)
        driver = staff_user(db)
        headers = token_for(app, driver)

        before = money_snapshot(db)
        resp = client.post(
            "/api/v1/staff/bottles/fine",
            json={
                "customer_id": u2.id,
                "address_id": a2.id,
                "quantity": -1,
                "fine_amount": 90000,
            },
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert "must be positive" in ((resp.get_json() or {}).get("message") or "").lower()
        db.session.rollback()
        assert BottleFine.query.count() == 0
        assert money_snapshot(db) == before


class TestFinesAcrossADissolve:
    """``_fine_scope`` freezes, and now FOLLOWS the freeze past a dissolve.

    UPDATED TWICE, and the sequence is the argument.

    The SURVIVOR's frozen fine scopes have followed the dissolve since Plan C —
    ``release_group_history_to_address`` re-stamps ``bottle_fines`` alongside
    ``bottle_ledger`` — so that case was closed and is pinned in
    ``test_place_conservation_invariants_e2e.py``.

    The case here is the OTHER one: the fine belongs to the DEPARTING address,
    whose frozen references §7.1/§7.3 deliberately never re-stamp (its ledger
    rows stay anchored to the group it left, because NULLing them would drop the
    place's history into a departed address's own scope and mint bottles onto
    someone who left with nothing). Its frozen scope is therefore a group with no
    members and no balance row.

    ``get_or_create_balance``'s CREATE branch runs ``assert_reachable``, which
    REFUSED (``BOTTLE_SCOPE_UNREACHABLE``) rather than re-INSERTing the row §7.3
    deleted. Silent stranding became a visible refusal — strictly better, and
    strictly not a full fix: the admin could not settle or waive such a fine at
    all.

    ``address_groups.dissolved_onto_address_id`` closes it. The dissolve records
    which address it released the place's history onto, and ``_fine_scope`` ->
    ``resolve_frozen_scope_for_write`` re-resolves THAT address's LIVE scope, so
    settling a departed member's fine debits the place that actually holds the
    crates. ``assert_reachable`` stays exactly where it was: a dissolve with no
    pointer still has nowhere honest to book, and is still refused by name (see
    ``TestAFineFrozenToAPlaceWithNoForwardingPointer`` below).
    """

    @staticmethod
    def _fine_then_dissolve(db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2)
        seed_place_bottles(db, admin, a1, 8)
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("6"),
            fine_amount=Decimal("300000"), actor_user_id=admin.id,
        )
        db.session.commit()
        result = CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        assert result["dissolved"] is True
        db.session.expire_all()
        # The whole place figure is on the SURVIVOR's own row now, and the
        # group's row is gone.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("8.00")
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        return admin, a2, group, fine

    def test_mark_fine_paid_after_the_place_dissolved_debits_the_survivor(self, db):
        """FIXED — the strict xfail is gone. The six bottles land on the survivor.

        WAS, in two stages. First: ``_fine_scope`` returned ``for_group(frozen
        group_id)`` unconditionally, so ``mark_fine_paid`` on a fine issued
        before §7.3 dissolved the place booked its -6 FINE_PAID through
        ``get_or_create_balance``, which RE-CREATED a ``bottle_balances`` row for
        the now-MEMBERLESS group: the survivor never lost the six bottles and
        ``orphaned_place_balances`` started firing again. Then:
        ``assert_reachable`` refused to mint that row, so nothing was stranded —
        and the fine could not be settled at all.

        NOW the frozen scope FOLLOWS ``dissolved_onto_address_id`` to the scope
        that actually holds the crates. Both halves are asserted below, because
        either alone is satisfiable by a wrong implementation: the survivor's
        figure alone would pass if an orphan row were ALSO minted, and the
        no-orphan check alone would pass if nothing were written at all.
        """
        admin, survivor, group, fine = self._fine_then_dissolve(db)
        # The pointer the settlement has to follow, written by the dissolve.
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id == survivor.id

        with MoneyFreeze(db, "paying a fine after the place dissolved"):
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
            db.session.commit()

        db.session.expire_all()
        # INTENDED: the six bottles come off the SURVIVOR's place.
        assert BottleTrackingService.get_place_balance(survivor.id) == Decimal("2.00")
        # ...and no unreachable group-keyed row was resurrected.
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        report = reconcile_customer_link_invariants()
        assert report["orphaned_place_balances"] == []
        assert report["negative_place_balances"] == []
        # The FINE_PAID entry is attributed to the survivor — scope and
        # attribution are ONE fact for an address scope — and keeps the door the
        # fine actually came through in its metadata.
        paid = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_PAID
        ).one()
        assert paid.address_id == survivor.id
        assert paid.address_group_id is None
        assert paid.entry_metadata["forwarded_from_place_group_id"] == group.id
        assert paid.entry_metadata["attributed_through_address_id"] == fine.address_id
        assert ledger_sum_for_place(survivor.id) == Decimal("2.00")

    def test_waive_fine_after_the_place_dissolved_creates_no_orphan_row(self, db):
        """FIXED — the strict xfail is gone. The waiver goes through, minting nothing.

        WAS, in two stages, same as the paid case. First: ``waive_fine`` on a
        fine whose place had dissolved re-created a 0.00 ``bottle_balances`` row
        for the memberless group — invisible to every balance assertion in this
        suite, visible only to ``orphaned_place_balances``, which started
        reporting a group §7.3 had already cleaned up. Then: ``assert_reachable``
        refused, so no phantom row was minted and the waiver was refused with it.

        A waive moves quantity 0, so no NUMBER can distinguish "waived correctly"
        from "waived and minted an orphan". The row's EXISTENCE is the only
        oracle, which is why this is pinned on the row rather than on a figure.
        """
        admin, survivor, group, fine = self._fine_then_dissolve(db)
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id == survivor.id

        with MoneyFreeze(db, "waiving a fine after the place dissolved"):
            BottleTrackingService().waive_fine(fine.id, actor_user_id=admin.id)
            db.session.commit()

        db.session.expire_all()
        assert BottleFine.query.get(fine.id).status == BottleFineStatus.WAIVED
        reversal = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_REVERSED
        ).one()
        assert Decimal(str(reversal.quantity)) == Decimal("0.00")
        # A waive moves no bottles at all...
        assert BottleTrackingService.get_place_balance(survivor.id) == Decimal("8.00")
        # ...and must not leave a phantom row behind the sweep will flag.
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []

    # ------------------------------------------------------------------ #
    # A LIVE place is not a dissolved one, and must not be forwarded.
    # ------------------------------------------------------------------ #

    def test_a_fine_frozen_to_a_place_that_is_STILL_LIVE_is_not_forwarded(self, db):
        """The control: forwarding must fire ONLY on a dissolve, never otherwise.

        The pointer is NULL for every live place, and ``_fine_scope`` must hand
        the frozen group back untouched — that is the original freeze guarantee,
        and widening it would split a FINE_ISSUED / FINE_PAID pair across two
        ledgers the first time an address left a place that kept going.

        A three-member place is used deliberately: removing one member leaves the
        place ALIVE, so the fine's frozen group still exists and still has a
        balance row, which is exactly the state the forwarding arm must not touch.
        """
        admin = make_user(db)
        u1, u2, u3 = make_user(db), make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u3)
        group = group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 8)
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("6"),
            fine_amount=Decimal("300000"), actor_user_id=admin.id,
        )
        db.session.commit()
        result = CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        assert result["dissolved"] is False, "the place must still be alive"
        db.session.expire_all()

        # No dissolve, so no pointer — on this group or on any other.
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id is None
        assert (
            AddressGroup.query.filter(
                AddressGroup.dissolved_onto_address_id.isnot(None)
            ).count()
            == 0
        )

        with MoneyFreeze(db, "paying a fine while the place is still live"):
            BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
            db.session.commit()

        db.session.expire_all()
        # The -6 landed in the FROZEN GROUP, untouched and un-forwarded.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("2.00")
        paid = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_PAID
        ).one()
        assert paid.address_group_id == group.id
        assert paid.address_id == a1.id, (
            "a NON-forwarded write is still attributed to the door it came through"
        )
        assert paid.entry_metadata.get("forwarded_from_place_group_id") is None, (
            "`audit()` must stay EMPTY for a write that was never forwarded"
        )
        # The departed address gained no scope of its own.
        assert BottleBalance.query.filter(
            BottleBalance.address_id == a1.id,
            BottleBalance.address_group_id.is_(None),
        ).count() == 0
        assert reconcile_customer_link_invariants()["orphaned_place_balances"] == []


class TestAFineFrozenToAPlaceWithNoForwardingPointer:
    """The refusal STAYS, and this is what still reaches it.

    ``BOTTLE_SCOPE_UNREACHABLE`` was the stopgap for a fine frozen to a place
    that dissolved. The forwarding pointer removes the ORDINARY route to it — a
    dissolve now leaves a destination — but two cases genuinely have none:

      * a place that dissolved BEFORE the column existed, whose audit row the
        migration's backfill could not resolve (missing, truncated, or the group
        gained members again), and
      * one whose survivor address has since been DELETED. The FK is
        ``ON DELETE SET NULL`` precisely so this degrades to "no destination"
        rather than to a dangling pointer.

    Booking either of those anywhere would be inventing a scope, which is the
    silent corruption the refusal replaced. NULLing the pointer reproduces both.
    """

    def test_settling_a_fine_with_a_NULL_pointer_is_REFUSED_by_name(self, db):
        """Nothing moves, nothing is minted, and the refusal keeps its name."""
        admin, survivor, group, fine = TestFinesAcrossADissolve._fine_then_dissolve(db)
        AddressGroup.query.filter_by(id=group.id).update(
            {AddressGroup.dissolved_onto_address_id: None}, synchronize_session=False
        )
        db.session.commit()
        db.session.expire_all()
        totals_before = bottle_totals(db)

        with MoneyFreeze(db, "paying a fine whose dissolved place has no pointer"):
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
            assert exc.value.error_code == "BOTTLE_SCOPE_UNREACHABLE"
            db.session.rollback()

        db.session.expire_all()
        assert bottle_totals(db) == totals_before
        assert BottleFine.query.get(fine.id).status == BottleFineStatus.PENDING
        assert BottleTrackingService.get_place_balance(survivor.id) == Decimal("8.00")
        # No orphan row was minted, and the sweep stays clean.
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        report = reconcile_customer_link_invariants()
        assert group.id not in report["orphaned_place_balances"]
        assert report["negative_place_balances"] == []
        assert ledger_sum_for_place(survivor.id) == Decimal("8.00")

    def test_waiving_a_fine_with_a_NULL_pointer_mints_NO_phantom_row(self, db):
        """A waive moves 0, so the ROW is the only oracle — pinned from both sides."""
        admin, survivor, group, fine = TestFinesAcrossADissolve._fine_then_dissolve(db)
        AddressGroup.query.filter_by(id=group.id).update(
            {AddressGroup.dissolved_onto_address_id: None}, synchronize_session=False
        )
        db.session.commit()
        db.session.expire_all()

        with MoneyFreeze(db, "waiving a fine whose dissolved place has no pointer"):
            with pytest.raises(ValidationError) as exc:
                BottleTrackingService().waive_fine(fine.id, actor_user_id=admin.id)
            assert exc.value.error_code == "BOTTLE_SCOPE_UNREACHABLE"
            db.session.rollback()

        db.session.expire_all()
        assert BottleFine.query.get(fine.id).status == BottleFineStatus.PENDING
        assert BottleTrackingService.get_place_balance(survivor.id) == Decimal("8.00")
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        report = reconcile_customer_link_invariants()
        assert group.id not in report["orphaned_place_balances"]


# --------------------------------------------------------------------------- #
# 10. THE COD CAP — membership-driven, bottle-blind
# --------------------------------------------------------------------------- #


class TestCodCapIsBottleBlind:
    """The single highest-consequence coupling in the feature: the cap decides
    whether a real customer can place an order AT ALL. If a bottle figure ever
    reaches it, an unreturned crate silently blocks checkout and the failure mode
    is a customer who simply cannot order.
    """

    def test_no_bottle_operation_moves_the_caps_place_arm(self, db):
        limit = CashCollectionService.COD_ACTIVE_DEBT_LIMIT
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2, a3)
        # One short of the cap, so a single phantom debt would flip the answer.
        for _ in range(limit - 1):
            delivered_cod_order(db, u2, address=a2, total=Decimal("10000.00"))
        service = CashCollectionService()
        baseline = service.get_cod_restriction_context(u1.id, delivery_address_id=a1.id)
        assert baseline["cod_restricted"] is False
        assert baseline["place_active_cod_debt_count"] == limit - 1

        bottles = BottleTrackingService()
        steps = []

        bottles.admin_adjust_balance(
            user_id=None, address_id=a1.id, adjustment=Decimal("500"),
            actor_user_id=admin.id, notes="500 crates at this place",
        )
        db.session.commit()
        steps.append(("adjust +500", service.get_cod_restriction_context(
            u1.id, delivery_address_id=a1.id)))

        fine = bottles.issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("400"),
            fine_amount=Decimal("1000000"), actor_user_id=admin.id,
        )
        db.session.commit()
        steps.append(("fine 1,000,000 UZS issued", service.get_cod_restriction_context(
            u1.id, delivery_address_id=a1.id)))

        bottles.mark_fine_paid(fine.id, actor_user_id=admin.id)
        db.session.commit()
        steps.append(("fine paid", service.get_cod_restriction_context(
            u1.id, delivery_address_id=a1.id)))

        CustomerLinkService().remove_address_from_group(
            a3.id, acting_admin_id=admin.id, reason="left", bottles_leaving=Decimal("50")
        )
        db.session.expire_all()
        steps.append(("split out 50", service.get_cod_restriction_context(
            u1.id, delivery_address_id=a1.id)))

        for label, context in steps:
            assert context == baseline, f"the COD cap moved after: {label}"
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("50.00")
        # The other half of the split's conservation pair: 500 adjusted in, 400
        # destroyed by the settled fine, 50 carried out with a3, 50 left behind.
        assert BottleTrackingService.get_place_balance(a3.id) == Decimal("50.00")
        assert UserAddress.query.filter_by(address_group_id=group.id).count() == 2

    def test_the_cap_changes_on_MEMBERSHIP_and_is_identical_for_any_bottle_count(self, db):
        """Proving the cap is membership-driven and bottle-blind requires varying
        bottles while holding membership FIXED. A test that only checks 'grouping
        restricts u2' cannot tell the two inputs apart.
        """
        limit = CashCollectionService.COD_ACTIVE_DEBT_LIMIT
        service = CashCollectionService()
        results = []
        for bottles_at_a1, bottles_at_a2 in (("0", "0"), ("7", "3"), ("999", "-42")):
            admin = make_user(db)
            u1, u2 = make_user(db), make_user(db)
            a1, a2 = make_address(db, u1), make_address(db, u2)
            for _ in range(limit):
                delivered_cod_order(db, u1, address=a1, total=Decimal("10000.00"))
            if bottles_at_a1 != "0":
                seed_place_bottles(db, admin, a1, bottles_at_a1)
            if bottles_at_a2 != "0":
                seed_place_bottles(db, admin, a2, bottles_at_a2)

            unrestricted = service.get_cod_restriction_context(
                u2.id, delivery_address_id=a2.id
            )
            assert unrestricted["cod_restricted"] is False
            assert unrestricted["place_active_cod_debt_count"] is None  # ungrouped

            with MoneyFreeze(db, f"grouping with bottles {bottles_at_a1}/{bottles_at_a2}"):
                group_addresses(db, admin, a1, a2)
            db.session.expire_all()

            after = service.get_cod_restriction_context(u2.id, delivery_address_id=a2.id)
            results.append(after)

        for after in results:
            assert after["cod_restricted"] is True
            assert after["restriction_scope"] == "place"
            assert after["place_active_cod_debt_count"] == limit
            assert after["active_cod_debt_count"] == 0  # u2 owes nothing herself
        # ...and the answer is IDENTICAL across wildly different bottle figures.
        assert results[0] == results[1] == results[2]


# --------------------------------------------------------------------------- #
# 11. THE ALLOCATION PLAN IS INDIFFERENT TO EVERY PLACE MUTATION
# --------------------------------------------------------------------------- #


def collection_outcome(db, *, payer, order_id, amount, admin):
    """Post a real collection and describe the money outcome structurally.

    Primary keys differ between two independently-built fixtures, so the
    comparison is on SHAPE and VALUES — which payments got how much, in what
    order, what remained unapplied, what every payment ended up owing — never on
    ids. ``scope_snapshot`` is reduced to its KEY SET and its member COUNTS for
    the same reason; the raw id lists inside it are fixture-specific and, on the
    ungroup variant, legitimately one address shorter (membership is the one
    input allowed to change a money read).
    """
    event = CashCollectionService().post_collection(
        customer_id=payer.id,
        amount=Decimal(str(amount)),
        source="standalone_meeting",
        order_id=order_id,
        recorded_by_user_id=admin.id,
        notes="lifecycle-indifference probe",
    )
    db.session.expire_all()
    event = CashCollectionEvent.query.get(event.id)
    allocations = (
        CashCollectionAllocation.query.filter_by(cash_collection_event_id=event.id)
        .order_by(CashCollectionAllocation.allocation_order.asc())
        .all()
    )
    snapshot = event.scope_snapshot or {}
    return {
        "unapplied": str(event.unapplied_amount),
        "scope_type": _stringify(event.scope_type),
        "scope_snapshot_keys": sorted(snapshot),
        "orderer_cluster_size": len(snapshot.get("orderer_cluster_user_ids") or []),
        "place_user_count": len(snapshot.get("place_user_ids") or []),
        "allocations": [
            {
                "order": int(a.allocation_order),
                "amount": str(a.allocated_amount),
                "mode": _stringify(a.allocation_mode),
                "is_own_order": a.order_id == order_id,
                "beneficiary_is_payer": a.beneficiary_user_id == payer.id,
                "source_is_payer": a.source_customer_id == payer.id,
                "reversed": a.reversed_at is not None,
            }
            for a in allocations
        ],
        "outstanding": sorted(
            str(p.outstanding_amount)
            for p in Payment.query.filter_by(user_id=payer.id).all()
        ),
    }


OWN_ORDER_ALLOCATION = {
    "order": 1,
    "amount": "35000.00",
    "mode": "auto",
    "is_own_order": True,
    "beneficiary_is_payer": True,
    "source_is_payer": True,
    "reversed": False,
}


class TestCollectionIsIndifferentToPlaceLifecycle:
    """A DIFFERENTIAL comparison alone cannot see a SYSTEMIC change.

    ``control == variant`` only fails when a place mutation treats the two
    fixtures differently; if the allocation engine stopped resolving place scope
    altogether — or started pooling on a different key — both sides would move
    together and the equality would still hold. Every test here therefore pins
    the control's ABSOLUTE shape as well: the scope type the engine resolved,
    the size of each frozen id set, and the exact amount that landed on which
    person's payment. The equality then proves indifference to the lifecycle,
    and the anchor proves the thing it is indifferent about is still real.
    """

    @staticmethod
    def _fixture(db):
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u1)
        group_addresses(db, admin, a1, a2, a3)
        seed_place_bottles(db, admin, a1, 6)
        order1, _p1 = delivered_cod_order(db, u1, address=a1, total=Decimal("35000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        return admin, u1, u2, a1, a2, a3, order1

    def test_a_split_does_not_change_the_allocation_plan(self, db):
        """The split writes ``bottle_balances`` rows and ``UserAddress``. If it
        touched an address the allocation scope reads, the candidate set for the
        collection would differ and the customer's money would land on a
        DIFFERENT order.
        """
        admin, u1, u2, _a1, _a2, _a3, order1 = self._fixture(db)
        coworker_payment_id = Payment.query.filter_by(user_id=u2.id).one().id
        coworker_before = money_snapshot(db)["payments"][coworker_payment_id]
        control = collection_outcome(
            db, payer=u1, order_id=order1.id, amount="35000.00", admin=admin
        )

        # ABSOLUTE ANCHOR. Place scope really was resolved (two owners, one
        # cluster of one) and an EXACT-cover collection settled the payer's own
        # order and nothing else. Without this, an engine that stopped resolving
        # place scope entirely would still satisfy the equality below.
        assert control["scope_type"] == "place"
        assert control["scope_snapshot_keys"] == [
            "address_ids",
            "group_id",
            "orderer_cluster_user_ids",
            "place_user_ids",
        ]
        assert control["place_user_count"] == 2
        assert control["orderer_cluster_size"] == 1
        assert control["unapplied"] == "0.00"
        assert control["outstanding"] == ["0.00"]
        assert control["allocations"] == [OWN_ORDER_ALLOCATION]
        # ATTRIBUTION: an exact cover must not touch the coworker's debt even
        # though place scope makes it reachable.
        assert money_snapshot(db)["payments"][coworker_payment_id] == coworker_before

        admin2, v1, _v2, _b1, _b2, b3, order_v1 = self._fixture(db)
        # b3 is u1's OWN second address at the place and carries no debts.
        CustomerLinkService().remove_address_from_group(
            b3.id, acting_admin_id=admin2.id, reason="left", bottles_leaving=Decimal("2")
        )
        db.session.expire_all()
        variant = collection_outcome(
            db, payer=v1, order_id=order_v1.id, amount="35000.00", admin=admin2
        )

        assert variant == control, (
            "a bottles_leaving split changed the money outcome of an otherwise "
            f"identical collection: control={control} variant={variant}"
        )
        assert control["allocations"], "the control fixture allocated nothing — vacuous"

    def test_a_merge_review_does_not_change_the_allocation_plan(self, db):
        """``_apply_merge_review`` runs AFTER ``_absorb_joiners_into_group`` and
        commits the whole join. If any part of it dirtied a Payment or an event
        (for example by autoflushing a stale identity-map object after
        ``expire_all``), the next collection would allocate against a mutated
        payment.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2 = make_address(db, u1), make_address(db, u2)
        seed_place_bottles(db, admin, a1, 6)
        seed_place_bottles(db, admin, a2, 4)
        order1, _p1 = delivered_cod_order(db, u1, address=a1, total=Decimal("35000.00"))
        _o2, p2 = delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))
        group_addresses(db, admin, a1, a2)
        control = collection_outcome(
            db, payer=u1, order_id=order1.id, amount="40000.00", admin=admin
        )

        # ABSOLUTE ANCHOR, and the one place in this file where money DOES cross
        # between two people: an over-collection under PLACE scope spills the
        # 5,000 surplus onto the COWORKER's delivered COD debt (spec §5, ring 1
        # — the engine's own design, resolved from MEMBERSHIP and never from a
        # bottle figure). Pinned by exact amount and exact beneficiary, so a
        # future change that made the spill bottle-dependent, or that redirected
        # it to prepaid credit, cannot hide behind a control==variant equality.
        assert control["scope_type"] == "place"
        assert control["place_user_count"] == 2
        assert control["unapplied"] == "0.00"
        assert control["outstanding"] == ["0.00"]
        assert control["allocations"] == [
            OWN_ORDER_ALLOCATION,
            {
                "order": 2,
                "amount": "5000.00",
                "mode": "auto",
                "is_own_order": False,
                "beneficiary_is_payer": False,
                "source_is_payer": True,
                "reversed": False,
            },
        ]
        db.session.expire_all()
        assert str(Payment.query.get(p2.id).outstanding_amount) == "15000.00"

        admin2 = make_user(db)
        v1, v2 = make_user(db), make_user(db)
        b1, b2 = make_address(db, v1), make_address(db, v2)
        bogus = seed_place_bottles(db, admin2, b1, 6)
        seed_place_bottles(db, admin2, b2, 4)
        order_v1, _pv1 = delivered_cod_order(db, v1, address=b1, total=Decimal("35000.00"))
        delivered_cod_order(db, v2, address=b2, total=Decimal("20000.00"))
        CustomerLinkService().create_place_group(
            [b1.id, b2.id],
            acting_admin_id=admin2.id,
            reason="counted the crates",
            excluded_ledger_entry_ids=[bogus.id],
            resulting_balance=Decimal("11"),
        )
        db.session.expire_all()
        assert BottleTrackingService.get_place_balance(b1.id) == Decimal("11.00")
        variant = collection_outcome(
            db, payer=v1, order_id=order_v1.id, amount="40000.00", admin=admin2
        )

        assert variant == control, (
            "a merge review changed the money outcome of an otherwise identical "
            f"collection: control={control} variant={variant}"
        )
        assert control["allocations"], "the control fixture allocated nothing — vacuous"


# --------------------------------------------------------------------------- #
# 12. §8 NETTING IS RETIRED — the one mechanism that DID move value
# --------------------------------------------------------------------------- #


class TestNettingIsGone:
    def test_the_removal_response_has_exactly_three_keys_and_no_netting(
        self, app, client, db, admin_auth_headers
    ):
        """§8 retired netting because it was the ONE mechanism that moved value
        between people on ungroup. A leftover key or a resurrected helper would
        be the most direct re-crossing of this boundary available.
        """
        admin = make_user(db)
        u1, u2 = make_user(db), make_user(db)
        a1, a2, a3 = make_address(db, u1), make_address(db, u2), make_address(db, u2)
        group = group_addresses(db, admin, a1, a2, a3)
        # An over-returned member (negative own sum) next to a positive place —
        # exactly the shape the old netting cousin acted on.
        seed_place_bottles(db, admin, a1, -4, notes="a1 over-returned")
        seed_place_bottles(db, admin, a2, 9, notes="a2's deliveries")
        delivered_cod_order(db, u1, address=a1, total=Decimal("15000.00"))
        delivered_cod_order(db, u2, address=a2, total=Decimal("20000.00"))

        before = money_snapshot(db)
        totals_before = bottle_totals(db)
        resp = client.delete(
            f"/api/v1/admin/place-groups/{group.id}/addresses/{a1.id}",
            json={"reason": "left"},
            headers=admin_auth_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]
        assert set(body) == {"place_group_id", "bottles_leaving", "dissolved"}
        # The three keys carry VALUES, and a label with no value asserted is a
        # test that cannot fail. a2 and a3 both remain, so nothing dissolved and
        # nothing left with a1.
        assert body["place_group_id"] == group.id
        assert Decimal(str(body["bottles_leaving"])) == Decimal("0.00")
        assert body["dissolved"] is False

        db.session.expire_all()
        # Bottles stayed with the PLACE by default; a1 left with nothing, and
        # its negative was NOT netted against the coworker's positive.
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("5.00")
        assert BottleTrackingService.get_place_balance(a1.id) == Decimal("0.00")
        assert bottle_totals(db) == totals_before
        assert money_snapshot(db) == before

    def test_no_netting_helper_survives_in_the_tree(self):
        import inspect

        from business_app.services import customer_link_service as cls_module

        source = inspect.getsource(cls_module)
        assert "_net_negative_pairs_on_removal" not in source
        assert not hasattr(CustomerLinkService, "_net_negative_pairs_on_removal")


# --------------------------------------------------------------------------- #
# 13. A BOTTLE FAILURE MUST NOT LEAVE MONEY DAMAGE BEHIND
# --------------------------------------------------------------------------- #


def _fully_prepaid_delivery_fixture(db):
    """A CASH order at a grouped address whose prepaid credit WILL settle it in
    full at DELIVERED, and whose bottle write WILL then raise.

    The bottle write is made to fail through real production configuration:
    ``BOTTLE_SESSION_ENFORCEMENT_STRICT`` turns a missing
    ``DriverBottleSessionOrder`` binding into a ``ValidationError``, which
    ``_handle_status_change_actions`` deliberately re-raises to abort the
    transition. Nothing is monkeypatched into the bottle service.
    """
    admin = make_user(db)
    u1, u2 = make_user(db), make_user(db)
    a1, a2 = make_address(db, u1), make_address(db, u2)
    group_addresses(db, admin, a1, a2)
    product = bottle_product(db, per_unit="2")
    order, payment = cash_order_with_bottles(
        db, u1, product, a1, quantity=2, status=OrderStatus.CONFIRMED, total="30000.00"
    )
    # Enough credit to settle the order in full at delivery -> COMPLETED ->
    # send_payment_confirmation_task.
    seed_unapplied_credit(db, u1, Decimal("30000.00"), admin)
    db.session.expire_all()
    return admin, u1, u2, a1, order, payment


class TestBottleFailureDoesNotDamageMoney:
    def test_a_validation_error_from_the_bottle_write_rolls_the_whole_transition_back(
        self, app, db
    ):
        """The bottle write's ``ValidationError`` is deliberately re-raised. The
        contract — stated in two comments in ``order_service.py`` — is that the
        whole transition then rolls back.

        This asserts that contract. It is the money boundary in its most
        dangerous direction: a BOTTLE-side failure deciding what happens to a
        customer's money and to a delivery's books.

        It used to fail. ``_handle_status_change_actions`` ran the bottle block
        LAST, after several COMMITTING calls
        (``consume_reserved_prepayment_for_payment`` /
        ``apply_customer_prepaid_credit_to_payment`` (@transactional),
        ``maybe_award_purchase_points(commit=True)``,
        ``LoyaltyService.update_streak(commit=True)``), so the flushed
        ``status=DELIVERED`` and the whole settlement were already durable by the
        time the bottle error was raised — and the ``delivery:{order_id}``
        idempotency key plus ``ORDER_STATUS_TRANSITIONS[DELIVERED] == []`` meant
        no retry could ever book those bottles. The block is now hoisted to the
        TOP of the DELIVERED branch (``OrderService._record_delivery_bottles``),
        so it aborts while there is still something to abort.
        """
        admin, u1, _u2, _a1, order, payment = _fully_prepaid_delivery_fixture(db)
        before = money_snapshot(db)
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError):
                OrderService().update_order_status(
                    order.id, OrderStatus.DELIVERED, updated_by=admin.id
                )
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False

        db.session.rollback()
        db.session.expire_all()
        # Either the transition rolls back whole, or it commits whole. What must
        # NEVER exist is a DELIVERED, fully-settled order with no bottle record.
        assert Order.query.get(order.id).status != OrderStatus.DELIVERED
        assert money_snapshot(db) == before
        # The customer's money is exactly where the fixture left it. Note WHERE
        # that is: the 30 000 is not *unapplied* credit and never was —
        # `post_collection` RESERVED it against this still-pending COD order at
        # seeding time, so the wallet's unapplied total reads 0.00 both before
        # and after, and the honest statement of "untouched" is that the
        # RESERVATION is still standing at its full amount. (`money_snapshot ==
        # before` above is the unfiltered proof that no money value moved
        # anywhere in the database; these two are the named specifics.)
        assert CashCollectionService().get_customer_prepaid_balance(u1.id) == Decimal(
            "0.00"
        )
        payment = Payment.query.get(payment.id)
        reserved = (payment.provider_data or {}).get("cod_prepayment_reserved_amount")
        assert Decimal(str(reserved or 0)) == Decimal("30000.00")
        assert str(payment.amount_collected) == "0.00"
        assert payment.status != PaymentStatus.COMPLETED
        assert BottleLedger.query.filter_by(order_id=order.id).count() == 0

    def test_the_aborted_delivery_can_be_retried_and_the_bottles_still_land(
        self, app, db
    ):
        """The companion, and the reason atomicity matters here rather than being
        a purist's point: because the transition rolls back WHOLE, the delivery
        is still retryable.

        This test used to pin the damage instead — a DELIVERED, fully-settled
        order with no bottle record, and a retry that could only ever raise
        because ``ORDER_STATUS_TRANSITIONS[DELIVERED] == []``. With the bottle
        block hoisted above the committing calls the order is left at its
        pre-image status, so the second attempt is a legal transition and the
        four bottles reach the place.
        """
        admin, u1, _u2, a1, order, payment = _fully_prepaid_delivery_fixture(db)
        place_before = BottleTrackingService.get_place_balance(a1.id)
        app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = True
        try:
            with pytest.raises(ValidationError):
                OrderService().update_order_status(
                    order.id, OrderStatus.DELIVERED, updated_by=admin.id
                )
        finally:
            app.config["BOTTLE_SESSION_ENFORCEMENT_STRICT"] = False
        db.session.rollback()
        db.session.expire_all()

        # Nothing settled, nothing booked, and the order is back where it was.
        assert Order.query.get(order.id).status == OrderStatus.CONFIRMED
        assert str(Payment.query.get(payment.id).amount_collected) == "0.00"
        assert BottleLedger.query.filter_by(order_id=order.id).count() == 0
        assert BottleTrackingService.get_place_balance(a1.id) == place_before

        # THE RETRY. Same order, same call — and now it goes through end to end:
        # the money settles from the reserved prepayment AND the place receives
        # the bottles, in one transaction.
        OrderService().update_order_status(
            order.id, OrderStatus.DELIVERED, updated_by=admin.id
        )
        db.session.expire_all()
        assert Order.query.get(order.id).status == OrderStatus.DELIVERED
        payment = Payment.query.get(payment.id)
        assert str(payment.amount_collected) == "30000.00"
        assert str(payment.outstanding_amount) == "0.00"
        assert payment.status == PaymentStatus.COMPLETED
        assert CashCollectionService().get_customer_prepaid_balance(u1.id) == Decimal(
            "0.00"
        )
        assert BottleLedger.query.filter_by(order_id=order.id).count() == 1
        assert BottleTrackingService.get_place_balance(a1.id) == place_before + Decimal(
            "4.00"
        )




# --------------------------------------------------------------------------- #
# 14. REAL POSTGRES — the two claims SQLite structurally cannot decide
# --------------------------------------------------------------------------- #


class TestOnRealPostgres:
    """Everything above runs on in-memory SQLite with FOREIGN KEYS OFF.

    Two of this file's claims are therefore undecidable there, and both are
    load-bearing for the triage of the fine-scope defect:

    1. ``bottle_ledger.user_id`` / ``bottle_fines.user_id`` are NOT NULL columns
       carrying a FOREIGN KEY, and the value is DERIVED (the place's
       lowest-id member's owner). With FKs off, a derivation that produced a
       bogus id would insert silently and every assertion above would still
       pass.
    2. The fine-after-dissolve row resurrection must be shown to be a REAL
       defect and not an artifact of FKs being off — a "bug" only reachable
       without foreign keys is not a production bug at all.

    Each test starts by PROVING foreign keys are actually enforced in this
    fixture. Without that control a green Postgres test would prove exactly as
    little as the SQLite ones.
    """

    @staticmethod
    def _place(pg_db):
        admin = make_user(pg_db)
        u1, u2 = make_user(pg_db), make_user(pg_db)
        a1, a2 = make_address(pg_db, u1), make_address(pg_db, u2)
        group = CustomerLinkService().create_place_group(
            [a1.id, a2.id], acting_admin_id=admin.id, reason="office", label="office"
        )
        return admin, u1, u2, a1, a2, group

    @staticmethod
    def _assert_foreign_keys_are_enforced(pg_db, address_id):
        """CONTROL. A dangling ``bottle_ledger.user_id`` must be REJECTED here.

        On the SQLite suite this insert succeeds, which is precisely why the two
        tests below cannot live there.
        """
        from sqlalchemy.exc import IntegrityError

        from sqlalchemy import func

        missing_user_id = (pg_db.session.query(func.max(User.id)).scalar() or 0) + 10_000
        pg_db.session.add(
            BottleLedger(
                user_id=missing_user_id,
                address_id=address_id,
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                quantity=Decimal("1"),
                balance_after=Decimal("1"),
                occurred_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            pg_db.session.flush()
        pg_db.session.rollback()

    def test_pg_derived_fine_attribution_points_at_a_real_user(self, pg_app, pg_db):
        """The audit stamp on a money-denominated object is a REAL user row.

        ``issue_fine(user_id=None)`` derives the stamp from the place's
        representative address owner. That derivation is the only thing standing
        between a fine and a NOT NULL foreign key, and the SQLite suite cannot
        tell a correct derivation from one that invented an id.
        """
        admin, _u1, _u2, a1, a2, group = self._place(pg_db)
        self._assert_foreign_keys_are_enforced(pg_db, a1.id)

        low_member = min([a1, a2], key=lambda a: a.id)
        fine = BottleTrackingService().issue_fine(
            user_id=None,
            address_id=a2.id,
            quantity=Decimal("6"),
            fine_amount=Decimal("300000"),
            actor_user_id=admin.id,
        )
        pg_db.session.commit()
        pg_db.session.expire_all()

        fine = BottleFine.query.get(fine.id)
        assert fine.user_id == low_member.user_id
        assert User.query.get(fine.user_id) is not None
        assert fine.address_group_id == group.id
        issued = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_ISSUED
        ).one()
        assert issued.user_id == low_member.user_id
        assert issued.address_group_id == group.id
        # A fine is still not money, under real constraints as under fake ones.
        assert Payment.query.count() == 0
        assert CashCollectionEvent.query.count() == 0

    def test_pg_fine_after_dissolve_FOLLOWS_the_pointer_and_mints_NO_orphan(
        self, pg_app, pg_db
    ):
        """UPDATED TWICE: the orphan is not minted, and the fine now settles.

        This test originally REPRODUCED the row resurrection with foreign keys ON
        and ``ck_bottle_balance_scope`` live, so it could not be dismissed as an
        FK-off artifact: the ``AddressGroup`` row is deliberately KEPT by §7.3,
        so the resurrected ``bottle_balances`` row satisfied every constraint the
        database has and NOTHING stopped it. That is exactly why the guard had to
        be in the application — the invariant "a grouped address has no own-scope
        balance row", and its mirror "a memberless group has no balance row at
        all", span two tables and cannot be expressed as a CHECK or a UNIQUE.

        Then it pinned ``assert_reachable`` REFUSING that write. Now it pins the
        forwarded write LANDING, on production's database, with those same
        constraints live: the -6 comes off the survivor's real row and the dead
        group's row is still not there. Both facts are needed — the guard is what
        makes the orphan impossible, and the pointer is what makes the settlement
        possible, and a fix that traded one for the other would be no fix.
        """
        admin, u1, _u2, a1, a2, group = self._place(pg_db)
        self._assert_foreign_keys_are_enforced(pg_db, a1.id)

        BottleTrackingService().admin_adjust_balance(
            user_id=None,
            address_id=a1.id,
            adjustment=Decimal("8"),
            actor_user_id=admin.id,
            notes="seed",
        )
        fine = BottleTrackingService().issue_fine(
            user_id=None,
            address_id=a1.id,
            quantity=Decimal("6"),
            fine_amount=Decimal("300000"),
            actor_user_id=admin.id,
        )
        pg_db.session.commit()

        result = CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        assert result["dissolved"] is True
        pg_db.session.expire_all()
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("8.00")
        # The forwarding pointer survived a real COMMIT under a real FK.
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id == a2.id

        BottleTrackingService().mark_fine_paid(fine.id, actor_user_id=admin.id)
        pg_db.session.commit()
        pg_db.session.expire_all()

        # The six bottles came off the place that HOLDS them...
        assert BottleTrackingService.get_place_balance(a2.id) == Decimal("2.00")
        # ...and the row the dissolve deleted STAYS deleted, even though the
        # AddressGroup it would point at is still there and every database
        # constraint would have accepted it.
        assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0
        assert AddressGroup.query.get(group.id) is not None
        report = reconcile_customer_link_invariants()
        assert group.id not in report["orphaned_place_balances"]
        assert report["negative_place_balances"] == []
        assert report["invalid_scope_balances"] == []
        assert report["stranded_address_balances"] == []
        # And still not one money row anywhere.
        assert Payment.query.count() == 0
        assert CashCollectionEvent.query.count() == 0
        # a1 is the lowest-id member, so the derived audit stamp is its owner —
        # and it points at a row that really exists under a live foreign key.
        assert BottleFine.query.get(fine.id).user_id == u1.id
        assert User.query.get(u1.id) is not None

    def test_pg_deleting_the_survivor_SET_NULLs_the_pointer_and_restores_the_refusal(
        self, pg_app, pg_db
    ):
        """``ON DELETE SET NULL`` is a DATABASE fact, undecidable on the fast suite.

        SQLite runs with foreign keys OFF, so every ondelete rule in this project
        is inert there: a test that deleted the survivor would leave the pointer
        DANGLING and prove the opposite of what it claimed. The whole reason the
        two refusals stay in the code is that this rule degrades a lost survivor
        to "no destination" rather than to a pointer at a row that is gone — so
        the rule itself has to be proven where it is actually enforced.

        A place with no bottles is used deliberately: the dissolve then mints no
        balance row for the survivor (§7.3 refuses to create a row for a date
        alone), which leaves the address free of FK children and genuinely
        deletable — the same state an ungrouped address reaches in production
        once its history has been cleared.
        """
        admin, _u1, _u2, a1, a2, group = self._place(pg_db)
        self._assert_foreign_keys_are_enforced(pg_db, a1.id)

        result = CustomerLinkService().remove_address_from_group(
            a1.id, acting_admin_id=admin.id, reason="left"
        )
        assert result["dissolved"] is True
        pg_db.session.commit()
        pg_db.session.expire_all()
        assert AddressGroup.query.get(group.id).dissolved_onto_address_id == a2.id

        # An UNGROUPED address is deletable — only a grouped one is fenced.
        pg_db.session.delete(UserAddress.query.get(a2.id))
        pg_db.session.commit()
        pg_db.session.expire_all()

        # THE RULE: the pointer is cleared by the database, not left dangling.
        group_row = AddressGroup.query.get(group.id)
        assert group_row is not None, "the dissolved group itself must survive"
        assert group_row.dissolved_onto_address_id is None

        # ...and a frozen write against that group falls back to the refusal it
        # had before the column existed. The funnel reports the dead end rather
        # than inventing a destination, and the guard behind it still fires by
        # name — asserted separately so it is unambiguous which one does what.
        target = BottleTrackingService.resolve_frozen_scope_for_write(a1.id, group.id)
        assert target.unreachable is True
        assert target.forwarded is False
        assert target.scope == BottleScope.for_group(group.id), (
            "the FROZEN scope must be handed back untouched, never a guess"
        )
        with pytest.raises(ValidationError) as exc:
            BottleTrackingService.assert_reachable(target.scope)
        assert exc.value.error_code == "BOTTLE_SCOPE_UNREACHABLE"
        pg_db.session.rollback()
