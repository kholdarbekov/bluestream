"""E2E JOURNEYS: the bottle/place lifecycle, walked by the people who live in it.

WHY THIS FILE IS NOT ANOTHER ``*_full_e2e.py``
----------------------------------------------
The eleven existing ``*_full_e2e`` modules test place LIFECYCLE MECHANICS: what
a join does to a ledger row, what a dissolve does to a balance row, which fence
refuses which input. They are thorough and they are not duplicated here.

What none of them does is walk an ACTOR through the SCREENS to a bottle outcome:

    a driver opens the door card and reads a number → he collects THAT number →
    where did the crates land, and does every other screen now agree?

A 4-lens review found five defects of exactly one shape — *a figure shown to a
human and the amount/scope posted to the engine decided by different code* — and
every one of them sat on that seam, between a screen and an action. None was
caught, because the suite is organised by COMPONENT: one file asserted the
figure, another asserted the engine, both passed, and the bug lived BETWEEN
them.

So every test below is a journey, and every journey ends on the same two
questions:

  1. **THE INVARIANT.** ``stored balance == ledger sum``, for every scope the
     journey touched — asserted through :func:`_pair`, which derives both
     figures from raw SQL over ``bottle_balances`` / ``bottle_ledger`` and
     imports NOTHING from ``business_app.services``. A balance-coupled write
     moves both by the same quantity; a write that moves only one is invisible
     to any single-sided assertion and can only ever be repaired by a merge
     review.

     THE ONE LEGITIMATE EXCEPTION is ``merge_backfill`` — the single
     balance-DECOUPLED writer in the codebase (``BALANCE_DECOUPLED_LEDGER_KEY
     _PREFIXES``). It is pinned explicitly in §8 rather than allowed to weaken
     the rule: every OTHER journey asserts that no decoupled row exists at all
     (:func:`_assert_no_decoupled_writes`), so if a second decoupled writer is
     ever added, these journeys go red before it reaches production.

  2. **EVERY SURFACE SHOWS THE SAME FIGURE.**
     :func:`_assert_every_surface_agrees` reads the same place through the real
     screens a human actually looks at —

        * the CUSTOMER bot: ``GET /orders/bottles/my-balances`` and, in §10,
          the history behind it, ``GET /orders/bottles/my-ledger/<address>``
        * the STAFF bot: ``GET /staff/bottles/customer/<id>/addresses`` and,
          where a delivery is in flight, the DOOR CARD's
          ``customer_bottle_balance`` from ``GET /staff/delivery/active``
        * the ADMIN bottle page: ``GET /admin/bottles/balances`` and
          ``GET /admin/bottles/ledger/<address>``

     — over real JWTs, and asserts all of them equal the ledger. A surface that
     disagrees with the ledger is the defect class this file exists to catch,
     and it cannot be seen by testing any one of them alone.

WHAT EACH SECTION WALKS
-----------------------
§1 the invariant, shown going RED against a corrupted world (the guard on the
guard) · §2 the door: a delivery, then a collection of exactly the figure the
card offered · §3 a place is created · §4 a member joins · §5 a member leaves,
with and without crates · §6 the place dissolves · §7 a fine issued before a
place change and settled after it, including across a dissolve (the forwarding
arm) · §8 a merge is reviewed — THE decoupled-writer exception · §9 the figure
read, the membership changed, the figure posted · §10 the history screen and
the balance screen · §11 the DDL-shaped journeys again on real Postgres,
including a collection racing an admin's membership edit, driven deterministically
with a held row lock rather than with threads.

THE STATE IS GENERATED, NOT HAND-PICKED
---------------------------------------
Every world comes from ``tests/integration/place_state_factory`` — usually the
``a6_canonical`` preset, whose defining property is bottles BOTH INSIDE a place
AND OUTSIDE it (6 pooled at office G, 2 at Alice's ungrouped home). That is the
divergent state: in a fixture where all the bottles are inside the place, "the
place's pool" and "everything this customer holds" are the SAME NUMBER, and a
surface that computes the wrong one is invisible. ``debt_inside_place_only`` is
built alongside in §2 as the control that proves the distinction is real.

CONVENTIONS
-----------
* No ``BottleBalance`` row is ever hand-built. Balances come from
  ``record_bottles_delivered`` / ``record_bottles_returned`` / the real staff
  HTTP routes / the real ``CustomerLinkService`` lifecycle, or from the factory
  (which writes each scope's row as the exact sum of the ledger rows it wrote).
* Conservation is asserted as a Σ over ALL scopes (``_stored_total`` /
  ``_ledger_total``), never as "the place ends on 7" — a membership edit that
  also stranded 4 bottles somewhere else passes the second and fails the first.
* One deliberate ledger deletion, in §8 only, to reproduce the documented
  dev-DB drift shape (a stored figure with no entries explaining it). It says so
  at the point of use.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import func

from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import (
    BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    BottleTrackingService,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    UserRole,
    UserType,
)
from tests.integration.place_state_factory import (
    AddressSpec,
    BottleSpec,
    DebtSpec,
    PersonSpec,
    PlaceSpec,
    ScenarioSpec,
    build,
    build_scenario,
)

pytestmark = pytest.mark.integration


# =========================================================================== #
# §0  HARNESS
# =========================================================================== #

_ZERO = Decimal("0.00")

# Disjoint from every phone block already in the tree: conftest owns
# +99890123456x, _scope_money_helpers +99890…, place_state_factory +99877…,
# test_staff_bot_place_full_e2e +9989011…/+9989022….
_STAFF_PHONE_PREFIX = "+99878"
_staff_seq = iter(range(1, 10_000))


def _staff(db, role, *, driver_profile=False):
    """A real staff account; drivers get the ``DeliveryPerson`` profile the
    ``require_staff_roles`` guard asserts is active."""
    n = next(_staff_seq)
    user = User(
        email=f"journey-staff-{n}@bluestream.test",
        phone=f"{_STAFF_PHONE_PREFIX}{n:06d}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Journey",
        last_name=role.value,
        user_type=UserType.STAFF,
        role=role,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    if driver_profile:
        db.session.add(
            DeliveryPerson(
                user_id=user.id,
                full_name="Journey Driver",
                phone=user.phone,
                email=user.email,
                is_active=True,
                is_available=True,
            )
        )
        db.session.commit()
    return user


def _url(app, path):
    """Absolute API path — the prefix lives in config, not in a literal."""
    return f"{app.config['API_PREFIX']}{path}"


def _headers(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _bottle_product(db, *, per_unit="1"):
    n = next(_staff_seq)
    category = ProductCategory(name=f"Journey Water {n}", description="w", is_active=True)
    db.session.add(category)
    db.session.commit()
    product = Product(
        name=f"Journey Pure Water {n}",
        description="d",
        category_id=category.id,
        size="19L",
        volume=19.0,
        volume_unit="L",
        base_price=Decimal("15000.00"),
        stock_quantity=100,
        min_stock_level=1,
        max_stock_level=500,
        is_active=True,
        tracks_returnable_bottles=True,
        returnable_bottles_per_unit=Decimal(str(per_unit)),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _delivery_at(db, customer, address, driver, *, product, quantity=1):
    """A CARD-paid, already-settled order ARRIVED at ``address``'s door.

    CARD + COMPLETED keeps the cash engine entirely out of this axis: the bottle
    figures on the card are what is under test, not COD.
    """
    n = next(_staff_seq)
    order = Order(
        user_id=customer.id,
        order_number=f"JRN-{n:06d}",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("15000.00"),
        delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"),
        loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"),
        payment_method=PaymentMethod.CARD,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("15000.00") * quantity,
        )
    )
    db.session.add(
        Payment(
            order_id=order.id,
            user_id=customer.id,
            payment_method=PaymentMethod.CARD,
            amount=order.total_amount,
            amount_collected=order.total_amount,
            outstanding_amount=_ZERO,
            currency="UZS",
            status=PaymentStatus.COMPLETED,
            payment_id=f"jrn_card_{n}",
        )
    )
    delivery = Delivery(
        order_id=order.id,
        delivery_person_id=driver.id,
        status=DeliveryStatus.ARRIVED,
        scheduled_date=datetime.now(UTC),
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.commit()
    return order, delivery


# -- the invariant ---------------------------------------------------------- #


def _scope_of(db, address_id):
    """``("group", g)`` or ``("address", a)`` — the place an address belongs to.

    Derived from ``addresses.address_group_id`` in raw ORM, deliberately NOT via
    ``BottleTrackingService.resolve_scope``: the oracle must not read its
    definition of a scope out of the code it is judging.
    """
    group_id = db.session.query(UserAddress.address_group_id).filter(
        UserAddress.id == address_id
    ).scalar()
    return ("group", group_id) if group_id is not None else ("address", address_id)


def _pair(db, address_id):
    """``(stored balance, ledger sum)`` for the place this address belongs to.

    The whole invariant of this file rests on these two numbers being computed
    from DIFFERENT tables by DIFFERENT aggregates. A write that moves one and
    not the other is exactly what a single-sided assertion cannot see.
    """
    kind, key = _scope_of(db, address_id)
    if kind == "group":
        stored_q = BottleBalance.query.filter(BottleBalance.address_group_id == key)
        ledger_f = [BottleLedger.address_group_id == key]
    else:
        stored_q = BottleBalance.query.filter(
            BottleBalance.address_id == key, BottleBalance.address_group_id.is_(None)
        )
        ledger_f = [BottleLedger.address_id == key, BottleLedger.address_group_id.is_(None)]
    row = stored_q.first()
    stored = Decimal(str(row.balance or 0)) if row is not None else _ZERO
    ledger = Decimal(
        str(
            db.session.query(func.coalesce(func.sum(BottleLedger.quantity), 0))
            .filter(*ledger_f)
            .scalar()
            or 0
        )
    )
    return stored.quantize(Decimal("0.01")), ledger.quantize(Decimal("0.01"))


def _assert_ledger_backed(db, address_id, expected=None, *, what=""):
    """``stored == ledger sum`` at this place, and (optionally) both == expected."""
    stored, ledger = _pair(db, address_id)
    assert stored == ledger, (
        f"{what}: place of address {address_id} is DRIFTED — "
        f"stored {stored} != ledger sum {ledger}. A balance-coupled write moves "
        f"both figures; only a merge_backfill may move one."
    )
    if expected is not None:
        assert stored == Decimal(str(expected)), f"{what}: expected {expected}, got {stored}"
    return stored


def _stored_total(db):
    return Decimal(
        str(db.session.query(func.coalesce(func.sum(BottleBalance.balance), 0)).scalar() or 0)
    ).quantize(Decimal("0.01"))


def _ledger_total(db):
    return Decimal(
        str(db.session.query(func.coalesce(func.sum(BottleLedger.quantity), 0)).scalar() or 0)
    ).quantize(Decimal("0.01"))


def _decoupled_rows(db):
    rows = []
    for prefix in BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES:
        rows.extend(
            BottleLedger.query.filter(BottleLedger.idempotency_key.like(f"{prefix}%")).all()
        )
    return rows


def _assert_no_decoupled_writes(db, *, what=""):
    """No ``merge_backfill`` row exists, so ``Σ stored == Σ ledger`` globally.

    Asserted in every journey EXCEPT §8, which is the one place a decoupled
    write is legitimate. Together the two halves say: the exception exists, and
    it exists in exactly one place.
    """
    rows = _decoupled_rows(db)
    assert rows == [], (
        f"{what}: a balance-DECOUPLED ledger row exists outside the merge review "
        f"({[r.idempotency_key for r in rows]}). Adding a second decoupled writer "
        f"breaks the stored==ledger invariant everywhere."
    )
    assert _stored_total(db) == _ledger_total(db), (
        f"{what}: Σ balances {_stored_total(db)} != Σ ledger {_ledger_total(db)}"
    )


# -- the surfaces ----------------------------------------------------------- #


def _customer_screen(client, app, customer, address_id_hint=None, *, place_group_id=None):
    """What the CUSTOMER's bot shows for one place: ``place_balance``.

    ``GET /api/orders/bottles/my-balances`` is keyed by place, so a row is
    matched on the place (group id, else address id) — never on the row's
    position, which sorts by balance and moves as the journey progresses.
    """
    resp = client.get(_url(app, "/orders/bottles/my-balances"), headers=_headers(app, customer))
    assert resp.status_code == 200, resp.get_json()
    rows = resp.get_json()["data"]["balances"]
    for row in rows:
        if place_group_id is not None:
            if row["place_group_id"] == place_group_id:
                return Decimal(str(row["place_balance"])).quantize(Decimal("0.01"))
        elif row["address_id"] == address_id_hint and row["place_group_id"] is None:
            return Decimal(str(row["place_balance"])).quantize(Decimal("0.01"))
    return None


def _staff_screen(client, app, driver, customer, address_id, *, place_group_id=None):
    """What the DRIVER's place picker shows for one PLACE.

    Matched on the PLACE, never on ``address_id``: this list is one row per
    place, and the ``address_id`` it carries is whichever of the customer's own
    addresses in that place has the lowest id — so a customer with two addresses
    at one office is deliberately NOT offered the door she used last time. A
    matcher keyed on the address would silently report "no row" and look like a
    missing place rather than a correctly deduplicated one.
    """
    resp = client.get(
        _url(app, f"/staff/bottles/customer/{customer.id}/addresses"), headers=_headers(app, driver)
    )
    assert resp.status_code == 200, resp.get_json()
    for row in resp.get_json()["data"]:
        if place_group_id is not None:
            if row["place_group_id"] == place_group_id:
                return Decimal(str(row["place_balance"])).quantize(Decimal("0.01"))
        elif row["address_id"] == address_id and row["place_group_id"] is None:
            return Decimal(str(row["place_balance"])).quantize(Decimal("0.01"))
    return None


def _admin_screen(client, app, admin, customer, *, place_group_id=None, address_id=None):
    """What the ADMIN bottle page shows for one place row."""
    resp = client.get(
        _url(app, f"/admin/bottles/balances?user_id={customer.id}&per_page=50"),
        headers=_headers(app, admin),
    )
    assert resp.status_code == 200, resp.get_json()
    for row in resp.get_json()["data"]["items"]:
        if place_group_id is not None and row["address_group_id"] == place_group_id:
            return Decimal(str(row["balance"])).quantize(Decimal("0.01"))
        if (
            address_id is not None
            and row["address_id"] == address_id
            and row["address_group_id"] is None
        ):
            return Decimal(str(row["balance"])).quantize(Decimal("0.01"))
    return None


def _customer_history(client, app, customer, address_id):
    """The customer's own place-history screen, as their bot renders it."""
    resp = client.get(
        _url(app, f"/orders/bottles/my-ledger/{address_id}?per_page=50"),
        headers=_headers(app, customer),
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["items"]


def _admin_history(client, app, admin, address_id):
    """The admin's place-history screen for the same place."""
    resp = client.get(
        _url(app, f"/admin/bottles/ledger/{address_id}?per_page=50"),
        headers=_headers(app, admin),
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]["items"]


def _door_card(client, app, driver, delivery_id):
    """The DRIVER's door card for one delivery, from the real active-list route."""
    resp = client.get(_url(app, "/staff/delivery/active"), headers=_headers(app, driver))
    assert resp.status_code == 200, resp.get_json()
    for item in resp.get_json()["data"]["items"]:
        if item["delivery_id"] == delivery_id:
            return item
    raise AssertionError(f"delivery {delivery_id} is not on the driver's active list")


def _assert_every_surface_agrees(
    db, client, app, *, customer, driver, admin, address_id, expected, what=""
):
    """The ledger, the customer's screen, the driver's screen and the admin's
    screen all show ONE number for this place.

    This is the assertion the component-organised suite structurally cannot
    make: each surface is read through its own real route, and the four figures
    are compared to each other AND to the ledger.
    """
    kind, key = _scope_of(db, address_id)
    stored = _assert_ledger_backed(db, address_id, expected, what=what)
    seen = {
        "ledger/stored": stored,
        "customer bot": _customer_screen(
            client,
            app,
            customer,
            address_id,
            place_group_id=key if kind == "group" else None,
        ),
        "staff bot": _staff_screen(
            client,
            app,
            driver,
            customer,
            address_id,
            place_group_id=key if kind == "group" else None,
        ),
        "admin page": _admin_screen(
            client,
            app,
            admin,
            customer,
            place_group_id=key if kind == "group" else None,
            address_id=address_id if kind == "address" else None,
        ),
    }
    disagreeing = {name: value for name, value in seen.items() if value != stored}
    assert not disagreeing, (
        f"{what}: the surfaces disagree about the place of address {address_id}. "
        f"ledger/stored={stored}, but {disagreeing}"
    )
    return stored


# =========================================================================== #
# §1  THE INVARIANT, AND PROOF IT CAN FAIL
# =========================================================================== #


class TestTheInvariantIsFalsifiable:
    """The guard on the guard.

    Every journey below leans on :func:`_assert_ledger_backed` and
    :func:`_assert_no_decoupled_writes`. If either can be satisfied by a broken
    world, nothing else in this file means anything — so both are shown going
    RED against a deliberately corrupted one.
    """

    def test_a_stored_balance_moved_without_a_ledger_row_is_caught(self, db):
        s = build(db, "a6_canonical")
        office = s.address("alice_office")
        _assert_ledger_backed(db, office.id, "6", what="baseline")

        # Move the STORED figure only — precisely what a balance-decoupled write
        # outside the merge review would do.
        row = BottleBalance.query.filter_by(address_group_id=s.place("g").id).one()
        row.balance = Decimal("99.00")
        db.session.commit()

        with pytest.raises(AssertionError, match="DRIFTED"):
            _assert_ledger_backed(db, office.id, what="corrupted")

    def test_a_ledger_row_written_without_moving_the_balance_is_caught(self, db):
        s = build(db, "a6_canonical")
        office = s.address("alice_office")

        db.session.add(
            BottleLedger(
                user_id=s.user("alice").id,
                address_id=office.id,
                address_group_id=s.place("g").id,
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                quantity=Decimal("3.00"),
                balance_after=Decimal("9.00"),
                occurred_at=datetime.now(UTC),
                idempotency_key="merge_backfill:sneaky",
                entry_metadata={},
            )
        )
        db.session.commit()

        with pytest.raises(AssertionError, match="DRIFTED"):
            _assert_ledger_backed(db, office.id, what="corrupted")
        with pytest.raises(AssertionError, match="balance-DECOUPLED"):
            _assert_no_decoupled_writes(db, what="corrupted")


# =========================================================================== #
# §2  JOURNEY: THE DOOR — a delivery happens, then the empties are collected
# =========================================================================== #


class TestTheDoorJourney:
    """A driver arrives at a shared office, reads the card, collects what it
    says, and everybody's screen has to agree afterwards.

    ``a6_canonical`` is the world on purpose: Alice holds crates BOTH at the
    office (pooled with Bob) AND at her own home. Those two numbers are 6 and 2
    — different — so a surface that answered "what does this customer hold"
    instead of "what does this PLACE hold" is visible here and invisible in a
    fixture where all the crates are inside the place.
    """

    def test_the_card_shows_the_place_pool_not_the_customers_total(self, db, app):
        s = build(db, "a6_canonical")
        alice, office, home = s.user("alice"), s.address("alice_office"), s.address("alice_home")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        product = _bottle_product(db)
        _, delivery = _delivery_at(db, alice, office, driver, product=product)

        card = _door_card(client, app, driver, delivery.id)

        # The place pool, NOT Alice's 8 crates across two places, and NOT the 4
        # she personally caused at the office.
        assert Decimal(str(card["customer_bottle_balance"])) == Decimal("6")
        assert Decimal(str(card["place_bottle_balance_signed"])) == Decimal("6")

        _assert_every_surface_agrees(
            db, client, app,
            customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="6", what="office, before the door",
        )
        _assert_every_surface_agrees(
            db, client, app,
            customer=alice, driver=driver, admin=admin,
            address_id=home.id, expected="2", what="home, before the door",
        )
        _assert_no_decoupled_writes(db, what="before the door")

    def test_collecting_exactly_what_the_card_offered_empties_that_place_and_only_that_place(
        self, db, app
    ):
        """THE SEAM. The figure the driver READ and the scope his POST SETTLED
        must be the same place — the bottle twin of the cash defect that paid
        one customer's debt out of another's collection."""
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, home = s.address("alice_office"), s.address("alice_home")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        product = _bottle_product(db)
        _, delivery = _delivery_at(db, alice, office, driver, product=product)

        offered = Decimal(str(_door_card(client, app, driver, delivery.id)["customer_bottle_balance"]))
        assert offered == Decimal("6")

        before_total = _stored_total(db)
        resp = client.post(
            _url(app, "/staff/bottles/collection"),
            headers=_headers(app, driver),
            json={
                "customer_id": alice.id,
                "address_id": office.id,
                "quantity": float(offered),
                "notes": "all returned",
            },
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()["data"]

        # The route's own echo is part of the screen: it tells the driver what is
        # LEFT, and it must be the same place it just debited.
        assert Decimal(str(body["quantity_collected"])) == offered
        assert Decimal(str(body["remaining_balance"])) == _ZERO

        _assert_every_surface_agrees(
            db, client, app,
            customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="0", what="office, after the collection",
        )
        # THE OTHER HALF. Alice's home crates are hers, at a different place, and
        # a scope that widened to the customer would have eaten them.
        _assert_every_surface_agrees(
            db, client, app,
            customer=alice, driver=driver, admin=admin,
            address_id=home.id, expected="2", what="home, after the collection",
        )
        assert _stored_total(db) == before_total - offered
        _assert_no_decoupled_writes(db, what="after the collection")

        # Bob is a coworker at the same place: he sees the same emptied pool,
        # because a place has ONE pool and not a slice per member.
        assert _customer_screen(client, app, bob, place_group_id=s.place("g").id) == _ZERO

    def test_the_control_where_all_the_crates_are_inside_the_place(self, db, app):
        """``debt_inside_place_only`` — kept deliberately by the factory as the
        world in which "the place's pool" and "everything the customer holds"
        are the SAME number. A journey green here and red on ``a6_canonical``
        has found the seam; one that only ever ran here proves nothing."""
        s = build(db, "debt_inside_place_only")
        # Give this world its crates through the real write path, all inside G.
        alice, office = s.user("alice"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        BottleTrackingService().record_bottles_delivered(
            order_id=s.order("alice_office").id,
            user_id=alice.id,
            address_id=office.id,
            quantity=Decimal("6"),
            actor_user_id=driver.id,
        )
        db.session.commit()

        _assert_every_surface_agrees(
            db, client, app,
            customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="6", what="control, one place only",
        )
        # The distinction the canonical world makes and this one cannot: here
        # there is nowhere else for a crate to be.
        assert _stored_total(db) == Decimal("6")

    def test_a_delivery_and_a_return_in_one_visit_land_on_one_place(self, db, app):
        """The ordinary visit: N crates left, M empties taken back, in the same
        trip. Both halves are the driver's, both must land in the pool, and the
        card's next reading has to be the arithmetic of the two."""
        s = build(db, "a6_canonical")
        alice, office = s.user("alice"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        service = BottleTrackingService()

        order = s.order("alice_office")
        service.record_bottles_delivered(
            order_id=order.id, user_id=alice.id, address_id=office.id,
            quantity=Decimal("3"), actor_user_id=driver.id,
        )
        db.session.commit()
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="9", what="after 3 delivered",
        )

        service.record_bottles_returned(
            user_id=alice.id, address_id=office.id, quantity=Decimal("4"),
            order_id=order.id, actor_user_id=driver.id,
        )
        db.session.commit()
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="5", what="after 4 returned",
        )
        _assert_no_decoupled_writes(db, what="one visit")


# =========================================================================== #
# §3  JOURNEY: A PLACE IS CREATED
# =========================================================================== #


_TWO_HOMES_ONE_OFFICE = ScenarioSpec(
    name="journey_two_homes_become_an_office",
    doc=(
        "Two coworkers who each hold crates at their own ungrouped address, and "
        "one of them ALSO holds crates at a second address that never joins. The "
        "third pile is what makes 'the place holds 7' distinguishable from "
        "'Alice holds 9'."
    ),
    people=(PersonSpec("alice"), PersonSpec("bob")),
    addresses=(
        AddressSpec("alice_office", owner="alice", title="work"),
        AddressSpec("bob_office", owner="bob", title="work"),
        AddressSpec("alice_home", owner="alice", title="home"),
    ),
    debts=(
        DebtSpec("alice_office", owner="alice", at="alice_office", amount="15000"),
        DebtSpec("bob_office", owner="bob", at="bob_office", amount="20000"),
    ),
    bottles=(
        BottleSpec("alice_crates", at="alice_office", quantity="4"),
        BottleSpec("bob_crates", at="bob_office", quantity="3"),
        BottleSpec("alice_home_crates", at="alice_home", quantity="2"),
    ),
)


class TestAPlaceIsCreatedJourney:
    def test_two_piles_become_one_pool_on_every_screen(self, db, app):
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        alice, bob = s.user("alice"), s.user("bob")
        a_office, b_office, a_home = (
            s.address("alice_office"), s.address("bob_office"), s.address("alice_home")
        )
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        # BEFORE: three separate places, three separate figures.
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="4", what="alice office, before",
        )
        _assert_every_surface_agrees(
            db, client, app, customer=bob, driver=driver, admin=admin,
            address_id=b_office.id, expected="3", what="bob office, before",
        )
        before_total = _stored_total(db)
        assert before_total == Decimal("9")

        group = CustomerLinkService().create_place_group(
            [a_office.id, b_office.id],
            acting_admin_id=admin.id,
            reason="same office",
            label="Journey Office",
        )
        db.session.expire_all()

        # AFTER: ONE pool of 7, seen identically by both members' bots, the
        # driver's picker and the admin page — and Alice's home is untouched.
        for viewer, member_address in ((alice, a_office), (bob, b_office)):
            _assert_every_surface_agrees(
                db, client, app, customer=viewer, driver=driver, admin=admin,
                address_id=member_address.id, expected="7",
                what=f"pooled place seen by {viewer.first_name}",
            )
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_home.id, expected="2", what="alice home, after the join",
        )

        # A JOIN MOVES CRATES; IT NEVER MINTS OR DESTROYS THEM.
        assert _stored_total(db) == before_total
        _assert_no_decoupled_writes(db, what="after the join")

        # The two absorbed address-scoped rows are GONE, not left stranded
        # alongside the group's row (which would double the crates in Σ).
        assert BottleBalance.query.filter(
            BottleBalance.address_id.in_([a_office.id, b_office.id])
        ).count() == 0
        assert BottleBalance.query.filter_by(address_group_id=group.id).one().balance == Decimal("7.00")

    def test_the_door_card_at_either_member_now_offers_the_pooled_total(self, db, app):
        """The screen change a driver actually experiences: yesterday Bob's door
        offered 3, today it offers 7 — because the crates behind that door are
        now one pool."""
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        alice, bob = s.user("alice"), s.user("bob")
        a_office, b_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        product = _bottle_product(db)

        _, bob_delivery = _delivery_at(db, bob, b_office, driver, product=product)
        assert Decimal(str(_door_card(client, app, driver, bob_delivery.id)["customer_bottle_balance"])) == Decimal("3")

        CustomerLinkService().create_place_group(
            [a_office.id, b_office.id], acting_admin_id=admin.id, reason="same office"
        )
        db.session.expire_all()

        assert Decimal(str(_door_card(client, app, driver, bob_delivery.id)["customer_bottle_balance"])) == Decimal("7")

        # And collecting THAT figure empties the pool — the seam again, this time
        # with the membership edit sitting between the read and the post.
        resp = client.post(
            _url(app, "/staff/bottles/collection"),
            headers=_headers(app, driver),
            json={"customer_id": bob.id, "address_id": b_office.id, "quantity": 7},
        )
        assert resp.status_code == 200, resp.get_json()
        assert Decimal(str(resp.get_json()["data"]["remaining_balance"])) == _ZERO
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="0", what="pool emptied through bob's door",
        )
        _assert_no_decoupled_writes(db, what="pool emptied")


# =========================================================================== #
# §4  JOURNEY: A MEMBER JOINS AN EXISTING PLACE
# =========================================================================== #


class TestAMemberJoinsJourney:
    def test_a_third_coworker_brings_their_crates_into_the_pool(self, db, app):
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, home = s.address("alice_office"), s.address("alice_home")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        before_total = _stored_total(db)
        assert before_total == Decimal("8")  # 6 pooled + 2 at Alice's home

        # Alice's OWN home address joins the office she already belongs to. The
        # joiner is hers, so the "two places, one cluster" degradation and the
        # coworker's view both stay in frame.
        CustomerLinkService().add_addresses_to_group(
            group_id=s.place("g").id,
            address_ids=[home.id],
            acting_admin_id=admin.id,
            reason="she moved her stock to the office",
        )
        db.session.expire_all()

        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="8", what="pool after the third address joined",
        )
        # Same physical place, reached through the address that just joined.
        assert _pair(db, home.id) == (Decimal("8.00"), Decimal("8.00"))
        assert _stored_total(db) == before_total
        _assert_no_decoupled_writes(db, what="after the join")

        # The coworker sees the enlarged pool too — one number, one place.
        assert _customer_screen(client, app, bob, place_group_id=s.place("g").id) == Decimal("8")

        # And the customer's own screen no longer offers a separate home row:
        # one place, one row, however many of her addresses are in it.
        resp = client.get(_url(app, "/orders/bottles/my-balances"), headers=_headers(app, alice))
        rows = resp.get_json()["data"]["balances"]
        assert [r["place_group_id"] for r in rows] == [s.place("g").id]


# =========================================================================== #
# §5  JOURNEY: A MEMBER LEAVES, TAKING CRATES
# =========================================================================== #


_THREE_AT_THE_OFFICE = ScenarioSpec(
    name="journey_three_at_the_office",
    doc=(
        "Three coworkers at ONE place holding 12 crates between them, so that a "
        "departure leaves a LIVE place behind instead of triggering the §7.3 "
        "dissolve. Cara also keeps a private address outside the place."
    ),
    people=(PersonSpec("ann"), PersonSpec("ben"), PersonSpec("cara")),
    places=(PlaceSpec("g", label="office"),),
    addresses=(
        AddressSpec("ann_office", owner="ann", place="g", title="work"),
        AddressSpec("ben_office", owner="ben", place="g", title="work"),
        AddressSpec("cara_office", owner="cara", place="g", title="work"),
        AddressSpec("cara_home", owner="cara", title="home"),
    ),
    debts=(DebtSpec("ann_office", owner="ann", at="ann_office", amount="12000"),),
    bottles=(
        BottleSpec("ann_crates", at="ann_office", quantity="5"),
        BottleSpec("ben_crates", at="ben_office", quantity="4"),
        BottleSpec("cara_crates", at="cara_office", quantity="3"),
        BottleSpec("cara_home_crates", at="cara_home", quantity="1"),
    ),
)


class TestAMemberLeavesJourney:
    def test_leaving_with_crates_moves_them_and_both_screens_follow(self, db, app):
        s = build_scenario(db, _THREE_AT_THE_OFFICE)
        ann, cara = s.user("ann"), s.user("cara")
        ann_office, cara_office, cara_home = (
            s.address("ann_office"), s.address("cara_office"), s.address("cara_home")
        )
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        _assert_every_surface_agrees(
            db, client, app, customer=cara, driver=driver, admin=admin,
            address_id=cara_office.id, expected="12", what="place before the departure",
        )
        before_total = _stored_total(db)

        result = CustomerLinkService().remove_address_from_group(
            cara_office.id, acting_admin_id=admin.id, reason="moved out", bottles_leaving=3
        )
        db.session.expire_all()
        assert result["dissolved"] is False  # two members remain: still a place

        # The place kept 9; Cara's address starts a fresh scope holding the 3 she
        # took. NOTHING was minted, nothing destroyed.
        _assert_every_surface_agrees(
            db, client, app, customer=ann, driver=driver, admin=admin,
            address_id=ann_office.id, expected="9", what="place after the departure",
        )
        _assert_every_surface_agrees(
            db, client, app, customer=cara, driver=driver, admin=admin,
            address_id=cara_office.id, expected="3", what="departed address",
        )
        _assert_every_surface_agrees(
            db, client, app, customer=cara, driver=driver, admin=admin,
            address_id=cara_home.id, expected="1", what="cara's untouched home",
        )
        assert _stored_total(db) == before_total
        _assert_no_decoupled_writes(db, what="after the departure")

        # Cara's own bot no longer shows the office pool at all — she cannot
        # reach those crates, and a screen that still offered 9 would invite a
        # driver to collect a stranger's empties.
        resp = client.get(_url(app, "/orders/bottles/my-balances"), headers=_headers(app, cara))
        rows = {r["address_id"]: r for r in resp.get_json()["data"]["balances"]}
        assert rows[cara_office.id]["place_group_id"] is None
        assert Decimal(str(rows[cara_office.id]["place_balance"])) == Decimal("3")

    def test_leaving_with_nothing_leaves_the_crates_with_the_place(self, db, app):
        """§7.1's default. The departing member's screen must drop to 0, and the
        place's must not move at all."""
        s = build_scenario(db, _THREE_AT_THE_OFFICE)
        ann = s.user("ann")
        ann_office, cara_office = s.address("ann_office"), s.address("cara_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        before_total = _stored_total(db)

        CustomerLinkService().remove_address_from_group(
            cara_office.id, acting_admin_id=admin.id, reason="moved out"
        )
        db.session.expire_all()

        _assert_every_surface_agrees(
            db, client, app, customer=ann, driver=driver, admin=admin,
            address_id=ann_office.id, expected="12", what="place keeps everything",
        )
        assert _pair(db, cara_office.id) == (_ZERO, _ZERO)
        # A departure must not MINT a 0.00 row for the leaver: that is the
        # `stranded_address_balances` shape the nightly sweep chases.
        assert BottleBalance.query.filter_by(address_id=cara_office.id).count() == 0
        assert _stored_total(db) == before_total
        _assert_no_decoupled_writes(db, what="default departure")


# =========================================================================== #
# §6  JOURNEY: THE PLACE DISSOLVES
# =========================================================================== #


class TestThePlaceDissolvesJourney:
    def test_the_last_member_out_takes_the_whole_pool_and_every_screen_follows(
        self, db, app
    ):
        """Two members, one leaves ⇒ §7.3 dissolves the place onto the survivor.

        The crates do not move physically; the SCOPE that holds them does. Every
        screen has to land on the survivor's own address, and the group's balance
        row — which no address can resolve to any more — must be gone.
        """
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, bob_office, home = (
            s.address("alice_office"), s.address("bob_office"), s.address("alice_home")
        )
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        group_id = s.place("g").id
        before_total = _stored_total(db)

        result = CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()
        assert result["dissolved"] is True

        # BOB is the survivor: he holds all 6, in his OWN scope now.
        _assert_every_surface_agrees(
            db, client, app, customer=bob, driver=driver, admin=admin,
            address_id=bob_office.id, expected="6", what="survivor after the dissolve",
        )
        # ALICE left with nothing at the office, and her home is untouched.
        assert _pair(db, office.id) == (_ZERO, _ZERO)
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=home.id, expected="2", what="alice's home after the dissolve",
        )

        assert _stored_total(db) == before_total
        _assert_no_decoupled_writes(db, what="after the dissolve")

        # The orphan class §7.3 exists to close: no balance row may survive for a
        # group nothing can resolve to.
        assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0

        # The admin page must not still be rendering the dead place.
        resp = client.get(
            _url(app, f"/admin/bottles/balances?user_id={bob.id}&per_page=50"),
            headers=_headers(app, admin),
        )
        rows = resp.get_json()["data"]["items"]
        assert [r["address_group_id"] for r in rows] == [None]
        assert Decimal(str(rows[0]["balance"])) == Decimal("6")

    def test_a_delivery_after_the_dissolve_lands_on_the_survivor_and_not_the_ghost(
        self, db, app
    ):
        """The place is gone; the door is not. The next delivery to the survivor
        must extend the SAME pile the dissolve handed him."""
        s = build(db, "a6_canonical")
        bob, office, bob_office = s.user("bob"), s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        group_id = s.place("g").id

        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()

        BottleTrackingService().record_bottles_delivered(
            order_id=s.order("bob_office").id,
            user_id=bob.id,
            address_id=bob_office.id,
            quantity=Decimal("4"),
            actor_user_id=driver.id,
        )
        db.session.commit()

        _assert_every_surface_agrees(
            db, client, app, customer=bob, driver=driver, admin=admin,
            address_id=bob_office.id, expected="10", what="delivery after the dissolve",
        )
        # Nothing was re-minted onto the dead group.
        assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0
        assert BottleLedger.query.filter(
            BottleLedger.address_group_id == group_id,
            BottleLedger.idempotency_key == f"delivery:{s.order('bob_office').id}",
        ).count() == 0
        _assert_no_decoupled_writes(db, what="delivery after the dissolve")


# =========================================================================== #
# §7  JOURNEY: A FINE ISSUED BEFORE A PLACE CHANGE, SETTLED AFTER IT
# =========================================================================== #


class TestAFineAcrossAPlaceChangeJourney:
    """A fine is a two-part episode: FINE_ISSUED when the driver writes it, and
    FINE_PAID (or FINE_REVERSED) whenever the customer settles — which can be
    weeks later, on the far side of a membership change.

    The scope is FROZEN at issue and the lifecycle CARRIES the frozen reference,
    so the two halves cannot end up in two different ledgers. That is only
    checkable by a journey: a component test of ``issue_fine`` and a component
    test of ``mark_fine_paid`` both pass while the pair is split.
    """

    def test_issued_ungrouped_then_grouped_then_paid_settles_inside_the_place(
        self, db, app
    ):
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        alice = s.user("alice")
        a_office, b_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        # 1. The driver fines Alice for 2 missing crates while her address is
        #    still ungrouped — through the real staff route.
        resp = client.post(
            _url(app, "/staff/bottles/fine"),
            headers=_headers(app, driver),
            json={
                "customer_id": alice.id,
                "address_id": a_office.id,
                "quantity": 2,
                "fine_amount": 40000,
                "notes": "two crates missing",
            },
        )
        assert resp.status_code in (200, 201), resp.get_json()
        fine = BottleFine.query.filter_by(user_id=alice.id).one()
        assert fine.address_group_id is None  # frozen to the ADDRESS
        # Issuing a fine moves no crates: the pile is still 4.
        _assert_ledger_backed(db, a_office.id, "4", what="after the fine was issued")

        # 2. The place is created. The frozen reference travels with the history.
        group = CustomerLinkService().create_place_group(
            [a_office.id, b_office.id], acting_admin_id=admin.id, reason="same office"
        )
        db.session.expire_all()
        assert BottleFine.query.get(fine.id).address_group_id == group.id
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="7", what="pooled place before the fine is paid",
        )

        # 3. The customer settles. The crates are accounted for, so the PLACE
        #    drops by the fine quantity — in the scope frozen at issue, which the
        #    join carried into the group.
        resp = client.put(
            _url(app, f"/admin/bottles/fines/{fine.id}"),
            headers=_headers(app, admin),
            json={"action": "mark_paid", "notes": "paid in cash"},
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()

        assert BottleFine.query.get(fine.id).status == BottleFineStatus.PAID
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="5", what="place after the fine was paid",
        )
        # BOTH HALVES OF THE EPISODE IN ONE LEDGER — the property a split pair
        # would break while leaving both balances individually plausible.
        episode = [
            e
            for e in BottleLedger.query.all()
            if (e.entry_metadata or {}).get("fine_id") == fine.id
        ]
        assert len(episode) == 2
        assert {e.address_group_id for e in episode} == {group.id}
        _assert_no_decoupled_writes(db, what="fine paid inside the place")

    def test_issued_inside_a_place_then_the_place_dissolves_then_paid_follows_the_crates(
        self, db, app
    ):
        """The forwarding arm. Alice's fine names a place that no longer exists;
        settling it must land on the scope that actually holds the crates —
        Bob's address — instead of re-minting the orphan the dissolve deleted."""
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, bob_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        group_id = s.place("g").id

        resp = client.post(
            _url(app, "/staff/bottles/fine"),
            headers=_headers(app, driver),
            json={
                "customer_id": alice.id,
                "address_id": office.id,
                "quantity": 2,
                "fine_amount": 40000,
            },
        )
        assert resp.status_code in (200, 201), resp.get_json()
        fine = BottleFine.query.filter_by(user_id=alice.id).one()
        assert fine.address_group_id == group_id  # frozen to the PLACE

        # Alice leaves ⇒ the place dissolves onto Bob, and the fine's frozen
        # reference now names a group with no members.
        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()
        assert BottleFine.query.get(fine.id).address_group_id == group_id
        before_total = _stored_total(db)

        resp = client.put(
            _url(app, f"/admin/bottles/fines/{fine.id}"),
            headers=_headers(app, admin),
            json={"action": "mark_paid"},
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()

        # The crates come off the SURVIVOR's pile, which is where they are.
        _assert_every_surface_agrees(
            db, client, app, customer=bob, driver=driver, admin=admin,
            address_id=bob_office.id, expected="4", what="survivor after the forwarded fine",
        )
        assert _stored_total(db) == before_total - Decimal("2")
        # And nothing was re-created for the dead group.
        assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0
        _assert_no_decoupled_writes(db, what="forwarded fine paid")

    def test_a_waived_fine_moves_no_crates_at_all(self, db, app):
        """The other settlement. A waiver is a MONEY decision; the crates are
        still missing, so no screen may move."""
        s = build(db, "a6_canonical")
        alice, office = s.user("alice"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        resp = client.post(
            _url(app, "/staff/bottles/fine"),
            headers=_headers(app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": 2, "fine_amount": 40000},
        )
        assert resp.status_code in (200, 201), resp.get_json()
        fine = BottleFine.query.filter_by(user_id=alice.id).one()

        resp = client.put(
            _url(app, f"/admin/bottles/fines/{fine.id}"),
            headers=_headers(app, admin),
            json={"action": "waive", "notes": "goodwill"},
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()

        assert BottleFine.query.get(fine.id).status == BottleFineStatus.WAIVED
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="6", what="place after the waiver",
        )
        _assert_no_decoupled_writes(db, what="waived fine")


# =========================================================================== #
# §8  JOURNEY: A MERGE IS REVIEWED — the ONE balance-decoupled writer
# =========================================================================== #


class TestAMergeIsReviewedJourney:
    """The admin counts the crates on site and states the truth.

    This is the ONLY journey in the file in which a ledger row moves without a
    balance moving with it: ``merge_backfill`` aligns a DRIFTED place's ledger to
    the figure the place already carries, so the exclusions and the override that
    follow move two figures that already agree. The exception is asserted here,
    by name, on both sides — and every other journey asserts it does not exist.
    """

    @staticmethod
    def _drifted(db, s, address_key):
        """Reproduce the documented dev-DB drift shape: a stored figure with NO
        ledger entries explaining it (dev address 24 — seeded before the ledger
        existed). This is the ONE place in the file that deletes ledger rows, and
        the balance row it leaves behind is the one the factory really wrote."""
        address = s.address(address_key)
        BottleLedger.query.filter_by(address_id=address.id).delete(synchronize_session=False)
        db.session.commit()
        stored, ledger = _pair(db, address.id)
        assert (stored, ledger) == (Decimal("4.00"), _ZERO)
        return address

    def test_the_preview_the_admin_decides_against_is_the_state_they_get(self, db, app):
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        alice, bob = s.user("alice"), s.user("bob")
        a_office, b_office = self._drifted(db, s, "alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        # THE SCREEN. The admin opens the merge preview before committing.
        resp = client.get(
            _url(app, f"/admin/place-groups/merge-preview?address_ids={a_office.id},{b_office.id}"),
            headers=_headers(app, admin),
        )
        assert resp.status_code == 200, resp.get_json()
        preview = resp.get_json()["data"]
        assert Decimal(str(preview["stored_balance"])) == Decimal("7")   # 4 drifted + 3 real
        assert Decimal(str(preview["computed_balance"])) == Decimal("3")  # the ledger only knows 3
        assert Decimal(str(preview["drift"])) == Decimal("4")
        assert Decimal(str(preview["projected_place_balance"])) == Decimal("7")

        # THE ACTION. The admin walks the office, counts 10 crates, states 10.
        group = CustomerLinkService().create_place_group(
            [a_office.id, b_office.id],
            acting_admin_id=admin.id,
            reason="counted on site",
            resulting_balance="10",
            preview_entry_ids=preview["entry_ids"],
        )
        db.session.expire_all()

        # THE STATE. Exactly what was stated, on every screen — AND the ledger
        # now explains it, which is the guarantee the backfill buys and which
        # was impossible while the alignment was written balance-coupled.
        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="10", what="reviewed merge",
        )
        assert _customer_screen(client, app, bob, place_group_id=group.id) == Decimal("10")

        # THE HISTORY SCREEN, which is where the borrowed attribution would
        # surface. `bottle_ledger.(user_id, address_id)` are NOT NULL, so a
        # PLACE-level correction has to borrow a member's stamp; presenting it
        # as that member's own would show a coworker an unexplained ±N they did
        # not cause and — since the customer view suppresses `notes` — cannot
        # account for. Both correction rows must therefore be unattributed.
        history = _customer_history(client, app, alice, a_office.id)
        assert sum(Decimal(str(row["quantity"])) for row in history) == Decimal("10")
        corrections = [
            row
            for row in history
            if row["member_name"] is None and row["is_own"] is False
        ]
        assert sorted(Decimal(str(row["quantity"])) for row in corrections) == [
            Decimal("3"),  # merge_correction: 10 stated vs 7 held
            Decimal("4"),  # merge_backfill: the drift the ledger never recorded
        ]

    def test_the_backfill_is_the_only_row_that_moved_a_ledger_without_a_balance(
        self, db, app
    ):
        """THE EXCEPTION, PINNED. Σ(balances after − before) equals the sum of
        the COUPLED quantities and nothing else; the backfill sits outside that
        sum by construction, and it is the only row that does."""
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        a_office, b_office = self._drifted(db, s, "alice_office"), s.address("bob_office")
        admin = _staff(db, UserRole.ADMIN)

        before_stored = _stored_total(db)
        before_ledger = _ledger_total(db)
        assert before_stored - before_ledger == Decimal("4")  # the drift, globally

        group = CustomerLinkService().create_place_group(
            [a_office.id, b_office.id],
            acting_admin_id=admin.id,
            reason="counted on site",
            resulting_balance="10",
        )
        db.session.expire_all()

        backfills = _decoupled_rows(db)
        assert len(backfills) == 1, "the merge review writes exactly one backfill"
        backfill = backfills[0]
        assert backfill.idempotency_key.startswith("merge_backfill:")
        assert Decimal(str(backfill.quantity)) == Decimal("4.00")
        assert (backfill.entry_metadata or {})["source"] == "merge_backfill"

        after_stored, after_ledger = _stored_total(db), _ledger_total(db)
        backfill_quantity = Decimal(str(backfill.quantity))

        # THE SPLIT, STATED AS ARITHMETIC. Every ledger quantity written in this
        # episode moved a balance by the same amount EXCEPT the backfill, so
        # subtracting exactly the backfill from the ledger's movement reproduces
        # the balances' movement. If a second decoupled writer appeared, or if
        # the backfill were ever made coupled, this equality is the thing that
        # breaks.
        assert (after_ledger - before_ledger) - backfill_quantity == after_stored - before_stored

        # ...and the drift is GONE, which is the whole point of the exception:
        # `stored == ledger` now holds globally, and on the reviewed place it
        # holds at exactly the figure the admin stated.
        assert after_stored == after_ledger
        assert _pair(db, a_office.id) == (Decimal("10.00"), Decimal("10.00"))

        # The one decoupled row belongs to THIS group's review, not to some
        # other place that happened to be in the database.
        assert backfill.address_group_id == group.id

    def test_restating_the_same_figure_is_a_no_op_that_still_leaves_the_two_agreeing(
        self, db, app
    ):
        """Convergence. A second review that states the number the place already
        holds must write no correction and must not re-open the drift."""
        s = build_scenario(db, _TWO_HOMES_ONE_OFFICE)
        alice = s.user("alice")
        a_office, b_office = self._drifted(db, s, "alice_office"), s.address("bob_office")
        a_home = s.address("alice_home")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        group = CustomerLinkService().create_place_group(
            [a_office.id, b_office.id], acting_admin_id=admin.id,
            reason="counted on site", resulting_balance="10",
        )
        db.session.expire_all()
        assert _pair(db, a_office.id) == (Decimal("10.00"), Decimal("10.00"))

        # Alice's ungrouped home joins later, and the admin re-states 10 + the
        # 2 crates it brings.
        CustomerLinkService().add_addresses_to_group(
            group_id=group.id, address_ids=[a_home.id],
            acting_admin_id=admin.id, reason="re-counted", resulting_balance="12",
        )
        db.session.expire_all()

        _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=a_office.id, expected="12", what="after the second review",
        )
        # Still exactly one backfill in the whole database: the second review had
        # no drift to align, because the first one closed it.
        assert len(_decoupled_rows(db)) == 1


# =========================================================================== #
# §9  JOURNEY: THE FIGURE READ, THE MEMBERSHIP CHANGED, THE FIGURE POSTED
# =========================================================================== #


class TestTheFigureReadAndTheScopePostedJourney:
    """The seam, in its sharpest form — and the one journey here whose outcome
    is a CHARACTERISATION rather than a contract.

    A driver opens the door card at a shared office and is offered N. Between
    that read and his POST, an admin removes the address from the place. The
    driver — standing at the door with N crates in his hands — posts N.

    Nothing on the collection path freezes the scope the figure was read
    against. ``record_standalone_collection`` re-resolves it at WRITE time
    (deliberately: a collection is a movement happening NOW, unlike a fine or an
    order correction, which belong to an earlier episode and DO freeze, via
    ``resolve_frozen_scope_for_write``). So the read and the write can describe
    two different places, and this journey measures exactly where the crates
    land.

    WHAT IS ASSERTED AS A CONTRACT: conservation. Six crates left the building,
    so Σ over every place drops by exactly six — the collection is neither lost
    nor double-counted.

    WHAT IS ASSERTED AS AN OBSERVATION, deliberately and with its consequence
    named: the six come off the DEPARTED address's brand-new scope (which held
    nothing), while the surviving place still reports six. Σ nets out, the
    per-place figures do not — the survivor's next door card will offer six
    crates that are already on a truck, and the departed address reads -6 on the
    customer's own screen.

    That is NOT pinned as a bug, because neither half of it is independently
    wrong: a negative place balance is a first-class, modelled state here (the
    door card carries ``place_bottle_balance_signed`` precisely to say
    "over-returned"), and attributing the pool to the last member out is §7.3's
    ruling, not a coding mistake. What the pair of them produces together is a
    seam worth a design decision, and the shape of it is recorded here so that
    decision is measurable when it is taken.
    """

    def test_a_collection_posted_after_the_place_moved_underneath_the_driver(
        self, db, app
    ):
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, bob_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        product = _bottle_product(db)
        _, delivery = _delivery_at(db, alice, office, driver, product=product)

        # 1. THE SCREEN.
        offered = Decimal(str(_door_card(client, app, driver, delivery.id)["customer_bottle_balance"]))
        assert offered == Decimal("6")
        before_total = _stored_total(db)

        # 2. THE WORLD MOVES. Alice leaves ⇒ the place dissolves onto Bob, who
        #    now holds all 6 in his own scope. Alice's address holds nothing —
        #    and a driver who REFRESHED would be offered 0 at that same door.
        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()
        assert _pair(db, office.id) == (_ZERO, _ZERO)
        assert _pair(db, bob_office.id) == (Decimal("6.00"), Decimal("6.00"))
        assert Decimal(str(_door_card(client, app, driver, delivery.id)["customer_bottle_balance"])) == _ZERO

        # 3. THE ACTION. The driver posts the figure he was shown, not the one he
        #    would have been shown had he refreshed.
        resp = client.post(
            _url(app, "/staff/bottles/collection"),
            headers=_headers(app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": float(offered)},
        )
        db.session.expire_all()

        if resp.status_code != 200:
            # A refusal would be a coherent answer too — the driver is told the
            # place changed and re-reads — but then NOTHING may have moved.
            assert _stored_total(db) == before_total
            return

        # 4. THE CONTRACT: conservation. Six crates left the customer's places,
        #    exactly once. Neither lost nor double-counted.
        assert _stored_total(db) == before_total - offered, (
            "the collection was accepted, so six crates left the customer's "
            f"places — but Σ balances went {before_total} -> {_stored_total(db)}"
        )
        assert _stored_total(db) == _ledger_total(db)
        _assert_no_decoupled_writes(db, what="collection across a membership change")

        # 5. THE OBSERVATION, with its consequence spelled out. The write
        #    resolved the scope LIVE, so the debit landed on the address the
        #    driver was standing at — which by then owned no crates at all.
        assert _pair(db, office.id) == (Decimal("-6.00"), Decimal("-6.00"))
        # ...while the survivor's pool is untouched, so those six crates are
        # simultaneously "on Bob's floor" and "over-returned at Alice's".
        assert _pair(db, bob_office.id) == (Decimal("6.00"), Decimal("6.00"))
        # Both readings reach a human unmodified: Alice's own bot shows the
        # negative, and Bob's next door card still offers the six.
        assert _customer_screen(client, app, alice, office.id) == Decimal("-6.00")
        assert _staff_screen(client, app, driver, bob, bob_office.id) == Decimal("6.00")

    def test_the_same_journey_with_a_refresh_between_the_read_and_the_post(self, db, app):
        """The control. A driver who re-reads the card before posting is offered
        0 and posts nothing — so the divergence above is caused by the STALE
        figure and by nothing else in the sequence."""
        s = build(db, "a6_canonical")
        alice = s.user("alice")
        office, bob_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        product = _bottle_product(db)
        _, delivery = _delivery_at(db, alice, office, driver, product=product)

        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()
        before_total = _stored_total(db)

        refreshed = Decimal(str(_door_card(client, app, driver, delivery.id)["customer_bottle_balance"]))
        assert refreshed == _ZERO
        # The driver collects what the refreshed card offers: nothing. The route
        # refuses a zero quantity at the boundary, and nothing moves.
        resp = client.post(
            _url(app, "/staff/bottles/collection"),
            headers=_headers(app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": float(refreshed)},
        )
        assert resp.status_code == 400, resp.get_json()
        assert _stored_total(db) == before_total
        assert _pair(db, office.id) == (_ZERO, _ZERO)
        assert _pair(db, bob_office.id) == (Decimal("6.00"), Decimal("6.00"))
        _assert_no_decoupled_writes(db, what="refreshed card, nothing posted")


# =========================================================================== #
# §10  JOURNEY: THE HISTORY SCREEN AND THE BALANCE SCREEN
# =========================================================================== #


class TestTheHistoryScreenAndTheBalanceScreenAgree:
    """Two screens, two endpoints, two tables — one number.

    A customer sees a BALANCE on one screen and a HISTORY on another, and the
    only thing tying them together is that the history is what produced the
    balance. Nothing in the codebase computes one from the other at read time,
    so nothing but a journey can check that they still describe the same place.
    This is where a scope that widened, narrowed or leaked shows up as an
    unexplained figure rather than as a failing unit test.
    """

    def test_the_history_a_customer_reads_sums_to_the_balance_they_are_shown(
        self, db, app
    ):
        s = build(db, "a6_canonical")
        alice, office = s.user("alice"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()
        service = BottleTrackingService()

        # Four kinds of movement, all through real write paths, so the history
        # is not one event type repeated.
        service.record_bottles_delivered(
            order_id=s.order("alice_office").id, user_id=alice.id, address_id=office.id,
            quantity=Decimal("5"), actor_user_id=driver.id,
        )
        db.session.commit()
        service.record_bottles_returned(
            user_id=alice.id, address_id=office.id, quantity=Decimal("3"),
            order_id=s.order("alice_office").id, actor_user_id=driver.id,
        )
        db.session.commit()
        client.post(
            _url(app, "/staff/bottles/collection"),
            headers=_headers(app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": 2},
        )
        service.admin_adjust_balance(
            user_id=None, address_id=office.id, adjustment=Decimal("-1"),
            actor_user_id=admin.id, notes="stock count",
        )
        db.session.commit()

        balance = _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="5", what="office after four movements",
        )

        history = _customer_history(client, app, alice, office.id)
        assert {row["event_type"] for row in history} == {
            "delivery", "return_on_delivery", "standalone_collection", "admin_adjustment",
        }
        assert sum(Decimal(str(row["quantity"])) for row in history) == balance

        # The admin's history of the same place sums to the same figure. Two
        # different serializers, two different routes, one place.
        assert sum(
            Decimal(str(row["quantity"])) for row in _admin_history(client, app, admin, office.id)
        ) == balance

    def test_one_pool_one_history_a_coworkers_movements_are_in_it(self, db, app):
        """Alice's history at the office contains BOB's delivery, named as his.

        A history scoped per-person would show her 4 while her balance screen
        showed the pool's 6 — two screens, one place, two numbers.
        """
        s = build(db, "a6_canonical")
        alice, bob, office = s.user("alice"), s.user("bob"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        balance = _assert_every_surface_agrees(
            db, client, app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="6", what="shared office",
        )
        history = _customer_history(client, app, alice, office.id)
        assert sum(Decimal(str(row["quantity"])) for row in history) == balance

        mine = [row for row in history if row["is_own"]]
        theirs = [row for row in history if not row["is_own"]]
        assert sum(Decimal(str(row["quantity"])) for row in mine) == Decimal("4")
        assert sum(Decimal(str(row["quantity"])) for row in theirs) == Decimal("2")
        assert {row["member_name"] for row in theirs} == {
            f"{bob.first_name} {bob.last_name}".strip()
        }

        # And Bob reads the SAME history — the pool has one story, not one per
        # member — with the ownership flags mirrored.
        bob_history = _customer_history(client, app, bob, s.address("bob_office").id)
        assert {row["id"] for row in bob_history} == {row["id"] for row in history}
        assert sum(Decimal(str(row["quantity"])) for row in bob_history) == balance

    def test_after_a_dissolve_each_party_reads_a_history_that_explains_their_own_balance(
        self, db, app
    ):
        """The departed member's screens must agree with each other AT ZERO, and
        must not carry a single row of the place she left — while the survivor's
        history explains every crate he inherited."""
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, bob_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = app.test_client()

        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()

        # ALICE: balance 0, history empty. Both true, and true TOGETHER — a
        # history that still listed the place's rows would explain a balance she
        # does not have, and would leak Bob's deliveries to her.
        assert _pair(db, office.id) == (_ZERO, _ZERO)
        alice_history = _customer_history(client, app, alice, office.id)
        assert alice_history == []

        # BOB: the whole pool, and a history that adds up to it — his own two
        # crates plus the four the dissolve released onto him.
        balance = _assert_every_surface_agrees(
            db, client, app, customer=bob, driver=driver, admin=admin,
            address_id=bob_office.id, expected="6", what="survivor",
        )
        bob_history = _customer_history(client, app, bob, bob_office.id)
        assert sum(Decimal(str(row["quantity"])) for row in bob_history) == balance
        # The release is attributed to the SURVIVOR — the person who now holds
        # the crates — because its stamp is derived from the surviving address's
        # own owner. (Contrast the merge review's corrections, which BORROW an
        # unrelated member's stamp to satisfy two NOT NULL columns and are
        # therefore suppressed from every member-facing view; that half is
        # asserted in §8's journey.)
        released = [row for row in bob_history if row["event_type"] == "admin_adjustment"]
        assert len(released) == 1
        assert Decimal(str(released[0]["quantity"])) == Decimal("4")
        assert released[0]["is_own"] is True
        assert released[0]["member_name"] == f"{bob.first_name} {bob.last_name}".strip()
        _assert_no_decoupled_writes(db, what="histories after the dissolve")


# =========================================================================== #
# §11  THE SAME JOURNEYS ON REAL POSTGRES
# =========================================================================== #


@pytest.mark.slow
class TestTheJourneysHoldOnRealPostgres:
    """SQLite makes ``with_for_update`` a no-op, ignores FOREIGN KEYs and never
    ran the migration chain, so a journey that is green there is silent about
    the three things the lifecycle actually rests on. These re-walk the two
    journeys whose invariants are DDL-shaped, on a fully migrated database.
    """

    def test_the_door_journey_on_postgres(self, pg_app, pg_db):
        db = pg_db
        s = build(db, "a6_canonical")
        alice, office, home = s.user("alice"), s.address("alice_office"), s.address("alice_home")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = pg_app.test_client()
        product = _bottle_product(db)
        _, delivery = _delivery_at(db, alice, office, driver, product=product)

        offered = Decimal(str(_door_card(client, pg_app, driver, delivery.id)["customer_bottle_balance"]))
        assert offered == Decimal("6")

        resp = client.post(
            _url(pg_app, "/staff/bottles/collection"),
            headers=_headers(pg_app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": float(offered)},
        )
        assert resp.status_code == 200, resp.get_json()

        _assert_every_surface_agrees(
            db, client, pg_app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="0", what="pg: office emptied",
        )
        _assert_every_surface_agrees(
            db, client, pg_app, customer=alice, driver=driver, admin=admin,
            address_id=home.id, expected="2", what="pg: home untouched",
        )
        _assert_no_decoupled_writes(db, what="pg door journey")

    def test_the_fine_across_a_dissolve_journey_on_postgres(self, pg_app, pg_db):
        """The forwarding arm needs REAL foreign keys: on SQLite a dangling
        ``dissolved_onto_address_id`` is indistinguishable from a live one."""
        db = pg_db
        s = build(db, "a6_canonical")
        alice, bob = s.user("alice"), s.user("bob")
        office, bob_office = s.address("alice_office"), s.address("bob_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = pg_app.test_client()
        group_id = s.place("g").id

        resp = client.post(
            _url(pg_app, "/staff/bottles/fine"),
            headers=_headers(pg_app, driver),
            json={"customer_id": alice.id, "address_id": office.id, "quantity": 2, "fine_amount": 40000},
        )
        assert resp.status_code in (200, 201), resp.get_json()
        fine = BottleFine.query.filter_by(user_id=alice.id).one()

        CustomerLinkService().remove_address_from_group(
            office.id, acting_admin_id=admin.id, reason="alice moved out"
        )
        db.session.expire_all()

        resp = client.put(
            _url(pg_app, f"/admin/bottles/fines/{fine.id}"),
            headers=_headers(pg_app, admin),
            json={"action": "mark_paid"},
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()

        _assert_every_surface_agrees(
            db, client, pg_app, customer=bob, driver=driver, admin=admin,
            address_id=bob_office.id, expected="4", what="pg: forwarded fine",
        )
        assert BottleBalance.query.filter_by(address_group_id=group_id).count() == 0
        _assert_no_decoupled_writes(db, what="pg forwarded fine")

    def test_a_collection_racing_a_membership_edit_is_refused_by_name_then_succeeds(
        self, pg_app, pg_db
    ):
        """THE RACE, driven deterministically — no threads, no sleeps.

        An admin is mid-way through removing Alice's address from the office.
        Its transaction is holding RUNG 1: the place's ``addresses`` rows,
        ``FOR NO KEY UPDATE``. At that exact instant the driver at the door posts
        his collection, which must take the same row ``FOR SHARE`` before it can
        resolve a scope.

        An INDEPENDENT connection stands in for the admin's half-finished
        transaction and holds the row; the request under test is bounded by
        ``BOTTLE_SCOPE_LOCK_TIMEOUT_MS``. Postgres raises 55P03 only if the
        service genuinely tried to take that row — so this is deterministic, and
        it is a claim SQLite cannot make at all (``with_for_update`` compiles to
        nothing there, and the same POST would sail straight through into a
        scope that is being edited underneath it).

        The journey has three beats, and the middle one is the point: the driver
        is REFUSED BY NAME (409 ``BOTTLE_SCOPE_LOCK_TIMEOUT``, which the staff
        bot renders as "try again in a moment"), NOTHING is written, and the
        identical retry after the admin finishes lands correctly.
        """
        import psycopg2

        db = pg_db
        pg_app.config["BOTTLE_SCOPE_LOCK_TIMEOUT_MS"] = 400
        s = build(db, "a6_canonical")
        alice, office = s.user("alice"), s.address("alice_office")
        driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
        admin = _staff(db, UserRole.ADMIN)
        client = pg_app.test_client()
        before_stored, before_ledger = _stored_total(db), _ledger_total(db)

        payload = {"customer_id": alice.id, "address_id": office.id, "quantity": 6}
        blocker = psycopg2.connect(pg_app.config["SQLALCHEMY_DATABASE_URI"])
        try:
            # RUNG 1, held — exactly what `remove_address_from_group` holds while
            # it decides whether the place dissolves.
            with blocker.cursor() as cur:
                cur.execute(
                    "SELECT id FROM addresses WHERE address_group_id = %s "
                    "ORDER BY id FOR NO KEY UPDATE",
                    (s.place("g").id,),
                )
                assert cur.fetchall(), "the blocker found no member rows to hold"

            resp = client.post(
                _url(pg_app, "/staff/bottles/collection"),
                headers=_headers(pg_app, driver),
                json=payload,
            )
            assert resp.status_code == 409, resp.get_json()
            # 409 + the NAMED code, not a 500 and not a bare "CONFLICT": the
            # staff bot resolves the driver's copy from `error_code` via
            # `API_ERROR_CODE_KEY_MAP`, so this string IS the screen.
            body = resp.get_json()
            assert body["error_code"] == "BOTTLE_SCOPE_LOCK_TIMEOUT", body
            assert body["details"]["address_id"] == office.id
        finally:
            blocker.rollback()
            blocker.close()

        # NOTHING MOVED. A refusal that had already written half of itself would
        # be far worse than the wait it replaced.
        db.session.rollback()
        db.session.expire_all()
        assert (_stored_total(db), _ledger_total(db)) == (before_stored, before_ledger)
        assert _pair(db, office.id) == (Decimal("6.00"), Decimal("6.00"))

        # THE RETRY, byte-identical, once the admin's transaction has ended.
        resp = client.post(
            _url(pg_app, "/staff/bottles/collection"),
            headers=_headers(pg_app, driver),
            json=payload,
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()
        _assert_every_surface_agrees(
            db, client, pg_app, customer=alice, driver=driver, admin=admin,
            address_id=office.id, expected="0", what="pg: retried collection",
        )
        assert _stored_total(db) == before_stored - Decimal("6")
        _assert_no_decoupled_writes(db, what="pg race then retry")
