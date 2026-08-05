"""The client retry token at the HTTP boundary, and the two claims only a real
Postgres can make.

TWO HALVES, DELIBERATELY SPLIT
------------------------------
§A runs on the fast SQLite fixtures and pins what the ROUTE contract is: a
duplicate POST is 200 (never 409 — a 409 would break the driver-facing flow), a
token replayed at a DIFFERENT door is 409 with a named code, and a malformed
token is 400 with a named code and leaves no row behind.

§B runs on `pg_app` / `pg_db` — a fresh database built by the real migration
chain. The two properties here are ones the SQLite suite structurally cannot
distinguish, and writing them on SQLite would assert semantics production never
has:

  * the CHECK-THEN-INSERT RACE. The idempotency `SELECT` holds no lock that
    excludes a peer, so two concurrent same-key writes both miss it and the
    btree UNIQUE makes the loser raise. `ExceptionMapper.EXCEPTION_MAPPING` has
    no SQLAlchemy entry, so without the rollback-and-requery fallback the loser
    is a 500 + CRITICAL — the retry-safety fix would turn a duplicate into an
    outage. The competing row here is inserted and COMMITTED by a genuinely
    separate connection, inside the real window, so the unique violation is a
    real one and not a simulated exception.
  * ATOMICITY. `issue_fine` writes the money (`bottle_fines`) and its audit
    trail (the FINE_ISSUED ledger row) and the two must commit together or not
    at all. This is the assertion that would have caught a `begin_nested()`
    "fix": measured in-container, a SAVEPOINT after SELECT-only work RELEASEs
    into a COMMIT on pysqlite, which would have split this method in two.

A third pin guards the ASSUMPTION the fallback rests on: rollback-and-requery is
only safe while both methods are entered TOP-LEVEL. If either ever becomes a
nested participant in a larger transaction, its `db.session.rollback()` would
silently discard its caller's work — so the call sites are pinned by name.
"""

import inspect
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import create_engine, text

from business_app.models.bottle import (
    BottleBalance,
    BottleFine,
    BottleLedger,
    DriverBottleSession,
)
from business_app.models.user import User, UserAddress
from business_app.services import bottle_tracking_service as bts_module
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    DriverBottleSessionStatus,
    UserRole,
    UserStatus,
    UserType,
)


TOKEN = "b7f1c93e2b0447a18e2d6c5f0a19d3e4"
OTHER_TOKEN = "c81d4fae7dec11d0a76500a0c91e6bf6"

COLLECTION_URL = "/api/v1/staff/bottles/collection"
FINE_URL = "/api/v1/staff/bottles/fine"

_SEQ = [0]


def _n() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


# --------------------------------------------------------------------------- #
# Seeding (engine-agnostic — used by both halves)
# --------------------------------------------------------------------------- #

def _user(sadb, *, role=UserRole.CUSTOMER, user_type=UserType.INDIVIDUAL) -> User:
    n = _n()
    user = User(
        phone=f"+99891{4000000 + n}",
        email=f"idem.{n}.{datetime.now(UTC).timestamp()}@example.com",
        first_name="Idem",
        last_name=f"User{n}",
        password_hash=hash_password("TestPassword123!"),
        role=role,
        user_type=user_type,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    sadb.session.add(user)
    sadb.session.commit()
    return user


def _driver(sadb) -> User:
    return _user(sadb, role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)


def _place(sadb, balance="10.00"):
    """A customer, their address, and the place balance seeded to `balance`."""
    owner = _user(sadb)
    n = _n()
    address = UserAddress(
        user_id=owner.id,
        title=f"Door {n}",
        full_address=f"{n} Test Street, Tashkent",
        city="Tashkent",
    )
    sadb.session.add(address)
    sadb.session.commit()
    sadb.session.add(BottleBalance(address_id=address.id, balance=Decimal(balance)))
    sadb.session.commit()
    return owner, address


def _session(sadb, driver: User) -> DriverBottleSession:
    row = DriverBottleSession(
        driver_user_id=driver.id,
        bottles_loaded=40,
        status=DriverBottleSessionStatus.OPEN,
    )
    sadb.session.add(row)
    sadb.session.commit()
    return row


def _headers(flask_app, user: User) -> dict:
    with flask_app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _balance_of(address_id: int) -> Decimal:
    row = BottleBalance.query.filter_by(address_id=address_id).one()
    return Decimal(str(row.balance))


def _collections(address_id: int):
    return BottleLedger.query.filter_by(
        address_id=address_id,
        event_type=BottleLedgerEventType.STANDALONE_COLLECTION,
    ).all()


# =========================================================================== #
# §A — the HTTP contract (SQLite)
# =========================================================================== #

@pytest.mark.integration
def test_a_duplicate_collection_post_dedupes_on_the_drivers_intent_token(app, db):
    """The token identifies the DECISION, not the transmission.

    Tested at the HTTP boundary rather than through the bot on purpose: after
    this change `StaffAPIClient` itself no longer re-POSTs an ambiguous failure,
    but a duplicate can still arrive from a proxy, a replay, or any future
    client. A driver who genuinely collects twice goes through the picker twice
    and mints a second token — which is why the key is per-intent and not a hash
    of the body.

    The second POST must stay **200**. A 409 here would surface to the driver as
    a failure for a collection that did land.
    """
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    client = app.test_client()

    body = {
        "customer_id": owner.id,
        "address_id": address.id,
        "quantity": 5,
        "notes": "retry",
        "idempotency_key": TOKEN,
    }
    headers = _headers(app, driver)
    for _ in range(2):
        assert client.post(COLLECTION_URL, json=body, headers=headers).status_code == 200

    db.session.expire_all()
    entries = _collections(address.id)
    assert len(entries) == 1
    # The stored key is COMPOSED server-side, so the driver cannot poison a
    # natural key such as `delivery:{order_id}`.
    assert entries[0].idempotency_key == f"collect:client:{driver.id}:{TOKEN}"
    assert _balance_of(address.id) == Decimal("5.00")


@pytest.mark.integration
def test_the_same_token_at_a_different_place_is_refused_not_silently_swallowed(app, db):
    """A dedup hit on the key ALONE is not proof of a replay.

    Without the post-fetch comparison a driver could reuse one token at every
    door: HTTP 200, no ledger row, no balance move and — since the tally moved
    inside the dedup fence — no session discrepancy either, so he keeps the
    bottles and the conservation oracle sees a consistent world.
    """
    driver = _driver(db)
    session = _session(db, driver)
    a_user, a_addr = _place(db, "5.00")
    b_user, b_addr = _place(db, "8.00")
    client = app.test_client()
    headers = _headers(app, driver)

    assert client.post(COLLECTION_URL, headers=headers, json={
        "customer_id": a_user.id, "address_id": a_addr.id, "quantity": 5,
        "idempotency_key": TOKEN,
    }).status_code == 200

    resp = client.post(COLLECTION_URL, headers=headers, json={
        "customer_id": b_user.id, "address_id": b_addr.id, "quantity": 8,
        "idempotency_key": TOKEN,
    })
    assert resp.status_code == 409
    assert (resp.get_json() or {}).get("error_code") == "BOTTLE_IDEMPOTENCY_KEY_REUSED"

    db.session.expire_all()
    assert _balance_of(b_addr.id) == Decimal("8.00")   # untouched
    assert _collections(b_addr.id) == []
    db.session.refresh(session)
    assert session.bottles_collected_from_customers == 5


@pytest.mark.integration
def test_a_duplicate_fine_post_issues_the_fine_once(app, db):
    """The money-carrying half. `issue_fine` was fenced by nothing at all.

    The replayed POST returns 200 with the ORIGINAL fine's `to_dict()`, so a
    client that lost the first response sees the answer it was owed rather than
    an error for a fine that was really issued.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    client = app.test_client()
    headers = _headers(app, driver)

    body = {
        "customer_id": owner.id,
        "address_id": address.id,
        "quantity": 2,
        "fine_amount": 30000,
        "notes": "missing empties",
        "idempotency_key": TOKEN,
    }
    first = client.post(FINE_URL, json=body, headers=headers)
    second = client.post(FINE_URL, json=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200

    db.session.expire_all()
    fines = BottleFine.query.filter_by(address_id=address.id).all()
    assert len(fines) == 1
    assert fines[0].idempotency_key == f"fine:client:{driver.id}:{TOKEN}"
    issued = BottleLedger.query.filter_by(
        address_id=address.id, event_type=BottleLedgerEventType.FINE_ISSUED
    ).all()
    assert len(issued) == 1
    # The response contract is unchanged: the same body, both times.
    assert first.get_json()["data"]["id"] == second.get_json()["data"]["id"]
    assert "idempotency_key" not in first.get_json()["data"]


@pytest.mark.integration
@pytest.mark.parametrize("bad", ["delivery:1", "merge_backfill:1:2", "short", "AAAAAAAA\n"])
def test_a_malformed_token_is_a_400_that_writes_nothing(app, db, bad):
    """Server-side pattern validation, at the boundary that matters.

    `"AAAAAAAA\\n"` is the `fullmatch` case: `^…$` + `re.match` accepts it, and
    the trailing newline would then be stored in the key column and emitted
    verbatim into the "Duplicate ledger entry skipped" log line.
    """
    driver = _driver(db)
    owner, address = _place(db, "0.00")
    client = app.test_client()
    headers = _headers(app, driver)

    resp = client.post(FINE_URL, headers=headers, json={
        "customer_id": owner.id, "address_id": address.id,
        "quantity": 2, "fine_amount": 30000, "idempotency_key": bad,
    })
    assert resp.status_code == 400
    assert (resp.get_json() or {}).get("error_code") == "BOTTLE_IDEMPOTENCY_KEY_INVALID"

    db.session.expire_all()
    assert BottleFine.query.filter_by(address_id=address.id).count() == 0


@pytest.mark.integration
def test_a_body_without_the_field_posts_exactly_as_it_did_before(app, db):
    """Backward compatibility, in both directions of a staggered deploy.

    Both routes read the body with `data.get(...)`, so an un-upgraded backend
    ignores the new key and an upgraded one treats its absence as the un-keyed
    path — which is what every internal caller and every legacy client uses.
    """
    driver = _driver(db)
    owner, address = _place(db, "10.00")
    client = app.test_client()
    headers = _headers(app, driver)

    body = {"customer_id": owner.id, "address_id": address.id, "quantity": 2}
    for _ in range(2):
        assert client.post(COLLECTION_URL, json=body, headers=headers).status_code == 200

    db.session.expire_all()
    entries = _collections(address.id)
    assert len(entries) == 2
    assert {e.idempotency_key for e in entries} == {None}
    assert _balance_of(address.id) == Decimal("6.00")


# =========================================================================== #
# §B — the claims that need a real Postgres
# =========================================================================== #

def _arm_competing_writer(monkeypatch, database_url: str, statement: str, params: dict):
    """Fire ONE competing INSERT from a SEPARATE, AUTOCOMMITTING connection.

    The hook hangs off `_utc_now`, which both methods call exactly once between
    their idempotency `SELECT` and the `flush()` that would violate the UNIQUE:
    `issue_fine` at `issued_at=self._utc_now()`, and the collection path at
    `_update_balance`'s `now = self._utc_now()`. That is precisely the window the
    race lives in, so the loser's unique violation here is a REAL one raised by
    Postgres against a REALLY concurrent committed row — not a simulated
    exception, and not a monkeypatched query that lies about what it found.
    """
    real_utc_now = BottleTrackingService._utc_now
    fired = {"n": 0}

    @staticmethod
    def _hook():
        if fired["n"] == 0:
            fired["n"] = 1
            engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
            try:
                with engine.connect() as conn:
                    conn.execute(text(statement), params)
            finally:
                engine.dispose()
        return real_utc_now()

    monkeypatch.setattr(BottleTrackingService, "_utc_now", _hook)
    return fired


@pytest.mark.integration
def test_a_lost_same_key_race_on_a_fine_returns_the_winners_row_not_a_500(
    pg_app, pg_db, ephemeral_pg_database, monkeypatch
):
    """THE CHECK-THEN-INSERT RACE, on the money-carrying write.

    Both racers miss the dedup `SELECT` (no lock excludes a peer) and the loser's
    INSERT hits `uq_bottle_fines_idempotency_key`. Without the fallback that is
    an unhandled `IntegrityError`, which `ExceptionMapper` maps to 500 +
    CRITICAL: the fix would have converted a duplicate into an outage.

    The fallback is rollback-and-requery rather than a SAVEPOINT precisely so
    that it is engine-uniform; the correctness of THAT choice is what
    `test_the_rollback_and_requery_fallback_assumes_both_methods_are_top_level`
    guards.
    """
    driver = _driver(pg_db)
    owner, address = _place(pg_db, "0.00")
    stored_key = f"fine:client:{driver.id}:{TOKEN}"

    _arm_competing_writer(
        monkeypatch,
        ephemeral_pg_database,
        """
        INSERT INTO bottle_fines
            (user_id, address_id, quantity, fine_amount, status, issued_by,
             issued_at, idempotency_key)
        VALUES
            (:user_id, :address_id, 2, 30000, 'pending', :issued_by,
             now(), :key)
        """,
        {"user_id": owner.id, "address_id": address.id,
         "issued_by": driver.id, "key": stored_key},
    )

    returned = BottleTrackingService().issue_fine(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("2"),
        fine_amount=Decimal("30000"),
        actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )

    pg_db.session.expire_all()
    fines = BottleFine.query.filter_by(address_id=address.id).all()
    assert len(fines) == 1, "the loser must not have inserted a second fine"
    assert returned.id == fines[0].id, "the loser must return the WINNER's row"
    assert fines[0].idempotency_key == stored_key


@pytest.mark.integration
def test_a_lost_same_key_race_on_a_collection_returns_one_row_and_one_tally(
    pg_app, pg_db, ephemeral_pg_database, monkeypatch
):
    """The same race on the collection path, with L2's tally invariant attached.

    The loser reaches `_assert_replay_matches_collection` with `created=False`,
    so the tally is skipped: a lost race must not credit the driver's session
    with bottles the winner already counted.
    """
    driver = _driver(pg_db)
    session = _session(pg_db, driver)
    owner, address = _place(pg_db, "10.00")
    stored_key = f"collect:client:{driver.id}:{TOKEN}"

    _arm_competing_writer(
        monkeypatch,
        ephemeral_pg_database,
        """
        INSERT INTO bottle_ledger
            (user_id, address_id, event_type, quantity, balance_after,
             actor_user_id, occurred_at, idempotency_key, entry_metadata)
        VALUES
            (:user_id, :address_id, 'standalone_collection', -3, 7,
             :actor, now(), :key, '{}')
        """,
        {"user_id": owner.id, "address_id": address.id,
         "actor": driver.id, "key": stored_key},
    )

    returned = BottleTrackingService().record_standalone_collection(
        user_id=owner.id,
        address_id=address.id,
        quantity=Decimal("3"),
        actor_user_id=driver.id,
        idempotency_key=TOKEN,
    )

    pg_db.session.expire_all()
    entries = _collections(address.id)
    assert len(entries) == 1, "the loser must not have inserted a second ledger row"
    assert returned.id == entries[0].id
    pg_db.session.refresh(session)
    assert session.bottles_collected_from_customers == 0, (
        "a deduped write must not bump the driver's session tally"
    )


@pytest.mark.integration
def test_the_fine_and_its_ledger_row_cannot_diverge(pg_app, pg_db, monkeypatch):
    """ONE transaction, or nothing. The assertion a savepoint would have broken.

    The money lives in `bottle_fines`; the audit trail lives in the FINE_ISSUED
    ledger row (which carries `quantity=0`). If the ledger write fails, the fine
    must not survive. Measured in-container, `begin_nested()` after SELECT-only
    work RELEASEs into a COMMIT on pysqlite — a savepoint-based race fallback
    would therefore have committed the fine while its ledger row ran in a
    separate transaction, manufacturing exactly the divergence this fix exists
    to prevent. On SQLite this test cannot tell one transaction from two, which
    is why it lives here.
    """
    driver = _driver(pg_db)
    owner, address = _place(pg_db, "0.00")

    def _explode(self, **kwargs):
        if kwargs.get("event_type") is BottleLedgerEventType.FINE_ISSUED:
            raise RuntimeError("ledger write failed")
        return BottleTrackingService._create_ledger_entry(self, **kwargs)

    monkeypatch.setattr(BottleTrackingService, "_create_ledger_entry", _explode)

    with pytest.raises(RuntimeError):
        BottleTrackingService().issue_fine(
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("2"),
            fine_amount=Decimal("30000"),
            actor_user_id=driver.id,
            idempotency_key=TOKEN,
        )

    pg_db.session.expire_all()
    assert BottleFine.query.filter_by(address_id=address.id).count() == 0
    assert BottleLedger.query.filter_by(address_id=address.id).count() == 0


@pytest.mark.integration
def test_the_new_unique_constraint_exists_in_the_MIGRATED_schema(pg_app, pg_db):
    """The MIGRATION half of the pair.

    Nothing in this suite compares models against migrations, so the model's
    `UniqueConstraint` (which `db.create_all()` gives the SQLite suite) does not
    cover for a missing migration. Production only ever sees this.
    """
    row = pg_db.session.execute(
        text(
            "SELECT contype FROM pg_constraint "
            "WHERE conname = 'uq_bottle_fines_idempotency_key'"
        )
    ).first()
    assert row is not None and row[0] == "u"

    col = pg_db.session.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'bottle_fines' AND column_name = 'idempotency_key'"
        )
    ).first()
    assert col is not None
    assert col[0] == "character varying"
    assert col[1] == 255
    assert col[2] == "YES"


@pytest.mark.integration
def test_nulls_stay_distinct_so_unkeyed_fines_are_unconstrained(pg_app, pg_db):
    """Why the constraint is PLAIN and not partial / `nulls_not_distinct`.

    Every server-initiated and legacy-client fine keeps a NULL key. If Postgres
    treated those NULLs as equal, the admin fine route — which mints no token —
    would 500 on its SECOND fine ever.
    """
    driver = _driver(pg_db)
    owner, address = _place(pg_db, "0.00")

    svc = BottleTrackingService()
    for _ in range(3):
        svc.issue_fine(
            user_id=owner.id,
            address_id=address.id,
            quantity=Decimal("1"),
            fine_amount=Decimal("1000"),
            actor_user_id=driver.id,
        )
    pg_db.session.expire_all()
    assert BottleFine.query.filter_by(address_id=address.id).count() == 3


# =========================================================================== #
# The assumption the fallback rests on
# =========================================================================== #

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEARCH_ROOTS = ("business_app", "staff_bot", "telegram_bot", "scripts", "shared")
_CALL_RE = re.compile(r"\.(record_standalone_collection|issue_fine)\s*\(")


@pytest.mark.integration
def test_the_rollback_and_requery_fallback_assumes_both_methods_are_top_level():
    """THE CONSTRAINT THIS FIX CREATES, made to fail loudly.

    `record_standalone_collection` and `issue_fine` handle the check-then-insert
    race with `db.session.rollback()` + a re-query. That is correct ONLY while
    both are entered TOP-LEVEL from a route: `@transactional`
    (`business_app/utils/transactions.py`) is a plain commit-on-exit /
    rollback-on-exception wrapper with NO nesting counter, so the only work the
    rollback discards is the method's own.

    The moment either becomes a nested participant in a larger transaction, that
    rollback silently discards its CALLER's work — an invisible data-loss bug.
    The honest fix then is a SAVEPOINT, and the pysqlite `RELEASE`-commits trap
    (documented workaround: `do_connect` -> `isolation_level = None`, `do_begin`
    -> `exec_driver_sql("BEGIN")`) must be solved first.

    So this pins the call sites BY NAME. A new caller turns this red on purpose:
    read the paragraph above before adding one.
    """
    callers = set()
    for root in _SEARCH_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel.startswith("business_app/migrations/"):
                continue          # migration prose, not calls
            if rel == "business_app/services/bottle_tracking_service.py":
                continue          # the definitions themselves
            if _CALL_RE.search(path.read_text(encoding="utf-8")):
                callers.add(rel)

    assert callers == {
        "business_app/api/staff.py",
        "business_app/api/admin_bottles.py",
    }, f"unexpected caller(s) of the two rollback-and-requery methods: {sorted(callers)}"


@pytest.mark.integration
def test_both_methods_are_still_wrapped_in_the_non_nesting_transactional():
    """The other leg of the same assumption.

    If `@transactional` were removed, the `db.session.rollback()` in the
    fallback would run with no transaction of its own to discard and the
    surrounding request would be left half-written instead.
    """
    source = inspect.getsource(bts_module.BottleTrackingService)
    for name in ("record_standalone_collection", "issue_fine"):
        match = re.search(rf"(@\w+\s*\n\s*)+def {name}\(", source)
        assert match, f"{name} not found"
        assert "@transactional" in match.group(0), f"{name} lost @transactional"

    # And `atomic_transaction` must stay a plain wrapper: a savepoint here would
    # re-introduce the pysqlite RELEASE-commits split the fallback avoids.
    # Asserted against the CODE OBJECT, not the source, because the docstring
    # legitimately mentions `begin_nested` as the thing callers should reach for
    # instead — a source scan would match that sentence and pass for the wrong
    # reason, or fail the day the sentence is reworded.
    from business_app.utils import transactions as tx_module

    generator_fn = tx_module.atomic_transaction.__wrapped__
    assert "begin_nested" not in generator_fn.__code__.co_names
    assert {"commit", "rollback"} <= set(generator_fn.__code__.co_names)
