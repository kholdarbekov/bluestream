"""L2 + L3: the session tally lives inside the dedup fence, and the client
retry token is validated, namespaced and payload-checked server-side.

WHAT IS UNDER TEST
------------------
L2 — ``record_standalone_collection`` used to call
``update_session_delivery_tally`` UNCONDITIONALLY, even when
``_create_ledger_entry`` had taken its idempotency short-circuit and returned a
PRE-EXISTING row without moving a single balance. The tally is a bare
read-modify-write with no idempotency of its own, so a correctly-deduped write
still credited the driver's session with bottles that were already counted — and
the damage is unrecoverable, because ``admin_adjust_balance`` repairs the
CUSTOMER's balance and no admin surface can touch
``bottles_collected_from_customers``.

L3 — the per-INTENT client token. Three fences, each pinned below:
  1. shape validation (``\\A…\\Z`` + ``fullmatch``, not ``^…$`` + ``match``),
  2. server-side namespacing, so the client never controls the whole key and
     cannot poison a natural key such as ``delivery:{order_id}``, and
  3. a POST-FETCH comparison of the dedup hit against the request, so one token
     replayed at another door is REFUSED instead of silently swallowed.

WHY SQLITE IS ENOUGH FOR THIS FILE
----------------------------------
Everything here is single-transaction semantics plus one model-level UNIQUE, and
``db.create_all()`` really does emit ``UNIQUE (idempotency_key)`` into
``sqlite_master`` (unlike FKs, which are silently OFF). The two claims SQLite
CANNOT make — the check-then-insert race fallback and "the fine and its ledger
row commit together or not at all" — live on the real-Postgres fixtures in
``tests/integration/test_bottle_idempotency_key.py``.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
    DriverBottleSession,
)
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ConfigurationError, ConflictError, ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    DriverBottleSessionStatus,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)


TOKEN = "b7f1c93e2b0447a18e2d6c5f0a19d3e4"
OTHER_TOKEN = "c81d4fae7dec11d0a76500a0c91e6bf6"

_SEQ = [0]


def _n() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# ---------------------------------------------------------------------------
# Fixtures-as-helpers (kept local; this file must not depend on another
# module's seeding conventions)
# ---------------------------------------------------------------------------

def _user(db, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL) -> User:
    n = _n()
    user = User(
        phone=f"+99890{5000000 + n}",
        email=f"fence.{n}@example.com",
        first_name="Fence",
        last_name=f"User{n}",
        password_hash=hash_password("TestPassword123!"),
        role=role,
        user_type=user_type,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.flush()
    return user


def _driver(db) -> User:
    return _user(db, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)


def _address(db, owner: User) -> UserAddress:
    n = _n()
    address = UserAddress(
        user_id=owner.id,
        title=f"Door {n}",
        full_address=f"{n} Test Street, Tashkent",
        city="Tashkent",
    )
    db.session.add(address)
    db.session.flush()
    return address


def _balance(db, address: UserAddress, qty) -> BottleBalance:
    """Ungrouped address => the place IS the address, so the row is address-keyed."""
    row = BottleBalance(address_id=address.id, balance=Decimal(str(qty)))
    db.session.add(row)
    db.session.flush()
    return row


def _place(db, qty="10.00"):
    """A customer + their address + a seeded place balance."""
    owner = _user(db)
    address = _address(db, owner)
    _balance(db, address, qty)
    return owner, address


def _session(db, driver: User) -> DriverBottleSession:
    session = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=40,
        status=DriverBottleSessionStatus.OPEN,
    )
    db.session.add(session)
    db.session.flush()
    return session


def _collections(address_id: int):
    return (
        BottleLedger.query.filter_by(
            address_id=address_id,
            event_type=BottleLedgerEventType.STANDALONE_COLLECTION,
        )
        .all()
    )


def _stored_balance(address_id: int) -> Decimal:
    row = BottleBalance.query.filter_by(address_id=address_id).one()
    return Decimal(str(row.balance))


# ===========================================================================
# L2 — the tally must not bump on a deduped write
# ===========================================================================

@pytest.mark.unit
def test_a_replayed_collection_writes_one_row_and_bumps_the_tally_once(db):
    """THE L2 ACCEPTANCE CRITERION, at the service boundary.

    The same intent token delivered twice is ONE collection: one ledger row, one
    balance move, and — the half that was broken — ONE tally bump. Before the
    fix the second call returned the pre-existing row (correct) and then bumped
    `bottles_collected_from_customers` anyway (wrong), so the driver's trip
    closed against a phantom surplus he could never clear.
    """
    driver = _driver(db)
    session = _session(db, driver)
    owner, address = _place(db, "10.00")
    db.session.commit()

    svc = BottleTrackingService()
    first = svc.record_standalone_collection(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )
    second = svc.record_standalone_collection(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )

    assert second.id == first.id
    assert len(_collections(address.id)) == 1
    assert _stored_balance(address.id) == Decimal("7.00")

    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 3


@pytest.mark.unit
def test_the_stored_key_is_composed_server_side_from_the_actors_id(db):
    """The client supplies a TOKEN, never a KEY.

    `uq_bottle_ledger_idempotency` is UNIQUE on the key alone and the dedup
    lookup carries no scope predicate, so a raw client key would be a global
    namespace shared with `delivery:{order_id}` and friends.
    """
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    db.session.commit()

    BottleTrackingService().record_standalone_collection(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("2"),
        actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )

    (entry,) = _collections(address.id)
    assert entry.idempotency_key == f"collect:client:{driver.id}:{TOKEN}"


@pytest.mark.unit
def test_a_different_intent_at_another_door_still_lands_and_tallies_twice(db):
    """The fence must not OVER-dedupe.

    A second, genuinely different collection carries a second token (a new flow
    mints a new uuid4), so both rows land and the tally counts both. This is the
    property a content-hash or time-bucketed key would have destroyed, and it is
    why `DECISION.md` rejected both.
    """
    driver = _driver(db)
    session = _session(db, driver)
    owner_a, addr_a = _place(db, "10.00")
    owner_b, addr_b = _place(db, "10.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=owner_a.id, address_id=addr_a.id, quantity=Decimal("3"),
        actor_user_id=driver.id, idempotency_key=TOKEN,
    )
    svc.record_standalone_collection(
        user_id=owner_b.id, address_id=addr_b.id, quantity=Decimal("4"),
        actor_user_id=driver.id, idempotency_key=OTHER_TOKEN,
    )

    assert len(_collections(addr_a.id)) == 1
    assert len(_collections(addr_b.id)) == 1
    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 7


@pytest.mark.unit
def test_two_unkeyed_collections_behave_exactly_as_before_the_fence_existed(db):
    """THE `idempotency_key is None` PATH, proved rather than asserted.

    `created` is True on every un-keyed write because the dedup branch lives
    inside `if idempotency_key:` — it is never entered, so the `SELECT` does not
    run and the function reaches its single success `return`. Two identical
    un-keyed collections are therefore still TWO collections and TWO tally
    bumps, which is what every internal caller, every legacy client and
    `test_no_admin_surface_can_reach_a_double_counted_session_tally` depend on.
    """
    driver = _driver(db)
    session = _session(db, driver)
    owner, address = _place(db, "10.00")
    db.session.commit()

    svc = BottleTrackingService()
    for _ in range(2):
        svc.record_standalone_collection(
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("3"),
            actor_user_id=driver.id,
        )

    entries = _collections(address.id)
    assert len(entries) == 2
    assert [e.idempotency_key for e in entries] == [None, None]
    assert _stored_balance(address.id) == Decimal("4.00")
    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 6


@pytest.mark.unit
def test_an_empty_string_token_is_the_unkeyed_path_not_a_stored_empty_key(db):
    """`""` is falsy, so it composes to None rather than to `collect:client:7:`.

    Without this a client that sends an empty field would mint ONE global key
    that every driver's first collection collides on.
    """
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    db.session.commit()

    entry = BottleTrackingService().record_standalone_collection(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("1"),
        actor_user_id=driver.id,
        idempotency_key="",
    )
    assert entry.idempotency_key is None


# ===========================================================================
# L3 fence 3 — the replay-payload comparison
# ===========================================================================

@pytest.mark.unit
def test_the_same_token_at_a_different_place_is_refused_not_swallowed(db):
    """A dedup hit on the key ALONE is not proof of a replay.

    Without the post-fetch comparison a driver could reuse one token at every
    door: the write returns 200 with someone else's row, no ledger row is
    written, no balance moves and — since L2 moved the tally inside the fence —
    no session discrepancy appears either. He keeps the bottles and the
    conservation oracle sees a consistent world.
    """
    driver = _driver(db)
    session = _session(db, driver)
    owner_a, addr_a = _place(db, "10.00")
    owner_b, addr_b = _place(db, "8.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=owner_a.id, address_id=addr_a.id, quantity=Decimal("5"),
        actor_user_id=driver.id, idempotency_key=TOKEN,
    )

    with pytest.raises(ConflictError) as excinfo:
        svc.record_standalone_collection(
            user_id=owner_b.id, address_id=addr_b.id, quantity=Decimal("8"),
            actor_user_id=driver.id, idempotency_key=TOKEN,
        )
    assert excinfo.value.error_code == "BOTTLE_IDEMPOTENCY_KEY_REUSED"

    assert _stored_balance(addr_b.id) == Decimal("8.00")   # untouched
    assert _collections(addr_b.id) == []
    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 5   # not 13


@pytest.mark.unit
def test_the_same_token_with_a_different_quantity_at_the_same_place_is_refused(db):
    """Quantity is part of the intent, so 5-then-9 on one token is a mismatch.

    Honouring it would let a driver record a 5-bottle pickup and then "confirm"
    a 9-bottle one that never touches the ledger.
    """
    driver = _driver(db)
    owner, address = _place(db, "20.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=owner.id, address_id=address.id, quantity=Decimal("5"),
        actor_user_id=driver.id, idempotency_key=TOKEN,
    )
    with pytest.raises(ConflictError) as excinfo:
        svc.record_standalone_collection(
            user_id=owner.id, address_id=address.id, quantity=Decimal("9"),
            actor_user_id=driver.id, idempotency_key=TOKEN,
        )
    assert excinfo.value.error_code == "BOTTLE_IDEMPOTENCY_KEY_REUSED"
    assert _stored_balance(address.id) == Decimal("15.00")


@pytest.mark.unit
def test_two_drivers_may_use_the_same_token_without_colliding(db):
    """`actor_user_id` is in the composed key, so tokens are per-driver.

    uuid4 makes a collision astronomically unlikely, but the key namespace is
    global and the consequence of a collision is a SILENTLY SUPPRESSED
    collection, so this is closed by construction rather than by probability.
    """
    driver_one = _driver(db)
    driver_two = _driver(db)
    owner, address = _place(db, "10.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        actor_user_id=driver_one.id, idempotency_key=TOKEN,
    )
    svc.record_standalone_collection(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        actor_user_id=driver_two.id, idempotency_key=TOKEN,
    )

    keys = {e.idempotency_key for e in _collections(address.id)}
    assert keys == {
        f"collect:client:{driver_one.id}:{TOKEN}",
        f"collect:client:{driver_two.id}:{TOKEN}",
    }


# ===========================================================================
# L3 fence 1 — shape validation
# ===========================================================================

@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "delivery:123",            # the natural-key poisoning attempt
        "merge_backfill:1:2",      # the balance-DECOUPLED namespace
        "collect:client:1:x" * 4,  # any ':' at all
        "short",                   # < 8
        "a" * 65,                  # > 64
        "has space",
        "has/slash",
        "AAAAAAAA\n",              # THE `fullmatch` PIN — `^…$` + `.match` accepts this
        "\nAAAAAAAA",
        "AAAA\nAAAA",
    ],
)
def test_a_malformed_token_is_refused_with_a_named_error_code(bad):
    """`fullmatch` with `\\A…\\Z`, never `match` with `^…$`.

    Measured: `re.compile(r"^[A-Za-z0-9_-]{8,64}$").match("AAAAAAAA\\n")` is
    True. Under a `match`-based check that trailing newline would be stored in
    `bottle_ledger.idempotency_key` and emitted verbatim through
    `logger.info("Duplicate ledger entry skipped: %s", …)` — a one-line
    log-forging primitive into the Loki stream.
    """
    with pytest.raises(ValidationError) as excinfo:
        BottleTrackingService.compose_client_idempotency_key("collect", 7, bad)
    assert excinfo.value.error_code == "BOTTLE_IDEMPOTENCY_KEY_INVALID"


@pytest.mark.unit
@pytest.mark.parametrize("absent", [None, "", 0, False])
def test_an_absent_token_composes_to_none(absent):
    """The un-keyed path, and the reason both columns stay nullable."""
    assert BottleTrackingService.compose_client_idempotency_key("collect", 7, absent) is None


@pytest.mark.unit
def test_a_well_formed_token_composes_into_its_namespace():
    compose = BottleTrackingService.compose_client_idempotency_key
    assert compose("collect", 7, TOKEN) == f"collect:client:7:{TOKEN}"
    assert compose("fine", 7, TOKEN) == f"fine:client:7:{TOKEN}"
    assert compose("collect", 7, "a" * 8) == "collect:client:7:" + "a" * 8
    assert compose("collect", 7, "a" * 64) == "collect:client:7:" + "a" * 64
    assert compose("collect", 7, "A-b_9" + "x" * 3) == "collect:client:7:A-b_9xxx"


@pytest.mark.unit
def test_the_actor_half_of_the_key_is_provably_an_integer():
    """`get_jwt_identity()` hands back a string `sub`, so this value is
    client-adjacent and must not be interpolated verbatim.

    The narrow claim first, because it is easy to overstate: `f"{'5'}"` and
    `f"{5}"` are the SAME string, so `int()` is not what makes a driver's own
    retry dedup — the production string identity already composes identically to
    an int one. What `int()` buys is that a non-integer identity RAISES here
    instead of reaching the key, which is the only route by which a ':' could
    enter the composed key from outside the token (the token's own pattern
    already forbids one). A key like `collect:client:5:6:<token>` would put the
    actor in control of a namespace separator.
    """
    compose = BottleTrackingService.compose_client_idempotency_key
    assert compose("collect", "5", TOKEN) == compose("collect", 5, TOKEN)
    with pytest.raises((ValueError, TypeError)):
        compose("collect", "5:6", TOKEN)
    with pytest.raises((ValueError, TypeError)):
        compose("collect", "not-an-id", TOKEN)


@pytest.mark.unit
def test_an_unknown_namespace_is_our_bug_and_maps_to_500_not_400():
    """Every call site passes a literal, so this can only be a programming error.

    A bare `ValueError` would map to 400 INVALID_VALUE and report our bug to the
    driver as their validation failure.
    """
    with pytest.raises(ConfigurationError):
        BottleTrackingService.compose_client_idempotency_key("delivery", 7, TOKEN)


@pytest.mark.unit
def test_a_client_keyed_collection_and_a_server_keyed_delivery_both_land(db):
    """Co-existence, NOT the namespacing fence — see the note below.

    A driver posts a collection with token T (stored `collect:client:{driver}:{T}`)
    and the REAL delivery of an order (stored `delivery:{order_id}`); both write
    their own row and both move the balance.

    HONEST SCOPE: this test does NOT prove the namespacing works. Every
    assertion here holds identically even if `compose_client_idempotency_key`
    returned the raw token — the two rows are distinct either way. The
    namespacing fence is pinned by `:217` (asserts the exact composed key
    `collect:client:{driver.id}:{TOKEN}`) and by the two-drivers-one-token case,
    and poisoning is structurally impossible anyway because
    `CLIENT_IDEMPOTENCY_TOKEN_PATTERN` forbids ':'. Renamed from
    `test_a_client_token_cannot_poison_the_delivery_natural_key`, whose docstring
    claimed a coverage this body never provided.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    order = Order(
        user_id=owner.id,
        order_number=f"ORD-FENCE-{_n():06d}",
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
    db.session.flush()
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_bottles_delivered(order.id, owner.id, address.id, Decimal("6"))
    db.session.commit()
    assert _stored_balance(address.id) == Decimal("6.00")

    # The driver's token is namespaced, so it cannot BE `delivery:{order_id}`.
    svc.record_standalone_collection(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        actor_user_id=driver.id, idempotency_key=TOKEN,
    )
    assert _stored_balance(address.id) == Decimal("4.00")

    delivery_row = BottleLedger.query.filter_by(
        idempotency_key=f"delivery:{order.id}"
    ).one()
    assert Decimal(str(delivery_row.quantity)) == Decimal("6.00")


# ===========================================================================
# The FINE half — the money-carrying write
# ===========================================================================

@pytest.mark.unit
def test_a_replayed_fine_issues_one_fine_and_one_ledger_row(db):
    """`issue_fine` had NO idempotency of any kind.

    The fence sits ABOVE the `BottleFine` construction because the money lives
    in `bottle_fines` — the FINE_ISSUED ledger row carries `quantity=0`, so
    keying the ledger alone would protect nothing.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    first = svc.issue_fine(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        fine_amount=Decimal("30000"), actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )
    second = svc.issue_fine(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        fine_amount=Decimal("30000"), actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )

    assert second.id == first.id
    assert BottleFine.query.filter_by(address_id=address.id).count() == 1
    assert first.idempotency_key == f"fine:client:{driver.id}:{TOKEN}"
    issued = BottleLedger.query.filter_by(
        address_id=address.id, event_type=BottleLedgerEventType.FINE_ISSUED
    ).all()
    assert len(issued) == 1


@pytest.mark.unit
def test_a_replayed_fine_token_with_a_different_amount_is_refused(db):
    """Money is part of the intent.

    Deliberately NOT status-gated in the other direction: if all four fields
    match, the replay IS the original intent and returning the original fine is
    the correct idempotent answer even after it was paid or waived.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.issue_fine(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        fine_amount=Decimal("30000"), actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )
    with pytest.raises(ConflictError) as excinfo:
        svc.issue_fine(
            user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
            fine_amount=Decimal("90000"), actor_user_id=driver.id,
            idempotency_key=TOKEN,
        )
    assert excinfo.value.error_code == "BOTTLE_IDEMPOTENCY_KEY_REUSED"
    assert BottleFine.query.filter_by(address_id=address.id).count() == 1


@pytest.mark.unit
def test_a_replayed_fine_token_at_another_place_is_refused(db):
    driver = _driver(db)
    owner_a, addr_a = _place(db, "0.00")
    owner_b, addr_b = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    svc.issue_fine(
        user_id=owner_a.id, address_id=addr_a.id, quantity=Decimal("2"),
        fine_amount=Decimal("30000"), actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )
    with pytest.raises(ConflictError):
        svc.issue_fine(
            user_id=owner_b.id, address_id=addr_b.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=driver.id,
            idempotency_key=TOKEN,
        )
    assert BottleFine.query.filter_by(address_id=addr_b.id).count() == 0


@pytest.mark.unit
def test_two_unkeyed_fines_still_both_land(db):
    """NULLs are DISTINCT under a plain UNIQUE on both engines.

    A partial `WHERE idempotency_key IS NOT NULL` index would therefore add
    nothing, and the admin route (which mints no token) is unaffected.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    for _ in range(2):
        svc.issue_fine(
            user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=driver.id,
        )
    assert BottleFine.query.filter_by(address_id=address.id).count() == 2


@pytest.mark.unit
def test_the_new_unique_constraint_is_really_created_by_create_all(db):
    """Pins the MODEL half of the migration.

    Nothing in this suite compares models against migrations (there is no
    `compare_metadata` drift test), and the SQLite suite builds its schema from
    `db.create_all()`. So this is the only thing that would notice if the
    `UniqueConstraint` were dropped from `__table_args__` while the migration
    stayed — the fine would silently duplicate again.
    """
    from sqlalchemy.exc import IntegrityError

    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    for _ in range(2):
        db.session.add(
            BottleFine(
                user_id=owner.id,
                address_id=address.id,
                quantity=Decimal("1"),
                fine_amount=Decimal("1000"),
                issued_by=driver.id,
                issued_at=datetime.now(UTC),
                idempotency_key="fine:client:1:duplicate-on-purpose",
            )
        )
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()


# ===========================================================================
# Ordering — the fence must stay BELOW the authz guards
# ===========================================================================

@pytest.mark.unit
def test_a_stranger_is_refused_by_scope_before_the_token_is_ever_composed(db):
    """`compose_client_idempotency_key` runs AFTER `_assert_user_in_scope`.

    Hoisting it above the scope guard would let a departed coworker's replay
    dedup into a 200 instead of being refused —
    `test_place_lifecycle_full_e2e.py:2749` pins the ordering for the keyless
    case and this pins it for the keyed one.
    """
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    stranger = _user(db)
    db.session.commit()

    svc = BottleTrackingService()
    svc.record_standalone_collection(
        user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
        actor_user_id=driver.id, idempotency_key=TOKEN,
    )

    # Same token, but the named member does not belong to the place: the scope
    # guard must fire, NOT the dedup short-circuit.
    with pytest.raises(ValidationError) as excinfo:
        svc.record_standalone_collection(
            user_id=stranger.id, address_id=address.id, quantity=Decimal("2"),
            actor_user_id=driver.id, idempotency_key=TOKEN,
        )
    assert excinfo.value.error_code == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"


@pytest.mark.unit
def test_a_malformed_token_is_refused_before_any_row_is_written(db):
    """Validation happens before the ledger write, so a 400 leaves no trace."""
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    db.session.commit()

    with pytest.raises(ValidationError):
        BottleTrackingService().record_standalone_collection(
            user_id=owner.id, address_id=address.id, quantity=Decimal("2"),
            actor_user_id=driver.id, idempotency_key="delivery:1",
        )
    assert _collections(address.id) == []
    assert _stored_balance(address.id) == Decimal("10.00")


# ===========================================================================
# The 16 server-keyed callers must be untouched
# ===========================================================================

@pytest.mark.unit
def test_the_one_value_adapter_still_returns_a_bare_ledger_row(db):
    """`_create_ledger_entry` keeps its contract for the other 16 call sites.

    The `created` flag is a NEW, opt-in second return value on
    `_create_ledger_entry_with_status`; `_create_ledger_entry` forwards
    `**kwargs` (the inner function is keyword-only, so forwarding is total) and
    unwraps. Several callers splat `**shared` into it and one reaches in from
    `order_edit_service`.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    entry = svc._create_ledger_entry(
        user_id=owner.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
        notes="direct call",
    )
    assert isinstance(entry, BottleLedger)
    assert Decimal(str(entry.quantity)) == Decimal("3")

    entry2, created = svc._create_ledger_entry_with_status(
        user_id=owner.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
        quantity=Decimal("1"),
        actor_user_id=driver.id,
    )
    assert isinstance(entry2, BottleLedger)
    assert created is True


@pytest.mark.unit
def test_a_server_keyed_replay_still_returns_the_prior_row_without_comparison(db):
    """§4.6b's comparison is deliberately NOT pushed into the shared helper.

    Sixteen callers derive their keys from durable business ids and several
    rely on the current "return whatever is there" semantics — pinned by
    `tests/integration/test_bottle_group_idempotency_and_adjust.py`. Confining
    the comparison to the two client-token methods keeps the new behaviour
    exactly where the new trust boundary is.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    db.session.commit()

    svc = BottleTrackingService()
    first, created_first = svc._create_ledger_entry_with_status(
        user_id=owner.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
        idempotency_key="server:derived:1",
    )
    # A DIFFERENT quantity under the SAME server key: no ConflictError, the
    # prior row comes back, and `created` is the flag that tells the caller so.
    second, created_second = svc._create_ledger_entry_with_status(
        user_id=owner.id,
        address_id=address.id,
        event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
        quantity=Decimal("99"),
        actor_user_id=driver.id,
        idempotency_key="server:derived:1",
    )
    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert _stored_balance(address.id) == Decimal("3.00")
