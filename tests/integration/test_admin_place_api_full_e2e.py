"""EVERY ADMIN ROUTE, EVERY PERMISSION TIER, EVERY ERROR CODE — end to end.

Axis: the HTTP surface an administrator actually touches for place-keyed bottle
state. Two route FAMILIES with two different permission vocabularies and two
different error envelopes:

  * `business_app/api/admin_bottles.py` — 17 routes, `validate_admin_action`
    with `view_orders` / `manage_orders`, failures through
    `handle_api_exception` -> `{error, message, error_code, status_code}`.
  * `business_app/api/admin.py`'s place-group family — 9 routes,
    `view_users` / `manage_users`, failures through `validation_error_response`
    -> `{success:false, message:'Validation failed', errors:[...],
    data:{error_code}}`.

That asymmetry is the axis's central finding and it is pinned from both ends: a
MANAGER may adjust a place's bottle balance and run the destructive Reconcile,
but may NOT form or edit a place; an OPERATOR may read every bottle screen and
write none of them, and cannot see the place family at all. None of that is
written down anywhere else in the tree, so every tier is a deliberate assertion
here rather than an accident of the decorator stack.

WHAT THIS FILE INSISTS ON

* **Real routes, real JWTs, real service write paths.** Balances are built with
  `record_bottles_delivered` / `record_bottles_returned` against real `Order`
  rows, with `admin_adjust_balance` / `set_initial_balance`, and through the
  real `CustomerLinkService` place lifecycle. No `BottleBalance` row is ever
  hand-constructed; the only hand-written balance mutation is `_manufacture_drift`,
  which DELETES a ledger row to reproduce the dev address-24 shape (stored 20,
  zero ledger rows) and says so at the point of use.
* **The pair, never one side.** Wherever a write could mint or destroy bottles,
  the global `Σ bottle_balances.balance` is captured before and after and
  asserted against the BALANCE-COUPLED ledger quantities the call appended.
  `merge_backfill:`-keyed rows are the one sanctioned decoupled writer and are
  excluded from that sum BY THEIR KEY.
* **A rejection writes NOTHING.** Every 4xx test re-reads the balance, the
  ledger row count and the row that was supposed to change, WITHOUT an
  intervening rollback — the rollback is exactly what would hide a
  flushed-but-uncommitted phantom.
* **Every test can fail.** Load-bearing ones were verified red by breaking the
  production behaviour and restoring it byte-identically (see the run notes).

PRODUCTION DEFECTS DEMONSTRATED HERE, marked `xfail(strict=True)` and named
`test_BUG_*` so they stay visible while the suite stays green:

  1. FIXED. `POST /admin/bottles/adjustment` and `/initial-balance` accepted an
     explicit OUT-OF-SCOPE `user_id` (no `_assert_user_in_scope`; `issue_fine`
     had it). The stranger's name landed on the place's ledger and then
     surfaced under `GET /bottles/ledger?user_id=<stranger>`. All three admin
     write bodies now share
     `BottleTrackingService._authorised_place_attribution`, so the fence is a
     funnel rather than a convention one caller can forget. `user_id` stays
     OPTIONAL: an ABSENT one is still derived from the place's representative
     address, and only an explicitly NAMED non-member is refused.
  2. FIXED by the same funnel. The same two routes accepted a `user_id` that
     does not exist at all, straight into a NOT NULL FK — a 500 on real
     Postgres, a committed dangling FK on this FK-off backend. A nonexistent
     user owns no address anywhere, so the membership fence stops it as a 400
     long before the FK.
  3. FIXED. No `is_finite()` guard on `adjustment` / `quantity` /
     `fine_amount`. The three routes failed in three DIFFERENT ways — verified
     rather than assumed, and the differences are why the guard could not be
     built out of the positivity checks that were already there:
       * `/adjustment` + `NaN` -> straight to the column. SQLite's NOT NULL
         rejects it (500); Postgres `numeric` ACCEPTS 'NaN' and the place's
         stored balance was poisoned permanently.
       * `/adjustment` + `±Infinity` -> a committed 200 on both backends;
         `reconcile_balance` could not undo it, because the ledger sum was
         non-finite too.
       * `/fines` + `NaN` -> `Decimal('NaN') <= 0` RAISES
         `decimal.InvalidOperation` (it does NOT evaluate to False — `decimal`
         is not IEEE-754), so the request died at the positivity guard with a
         500 and nothing was persisted. `/fines` + `Infinity` was the case that
         really did slip past both guards, persist, and poison the place when
         the fine was settled — while `-Infinity` was already caught, since
         `Decimal('-Infinity') <= 0` is True. The two signs are NOT symmetric.
     The refusal now lives in BOTH layers on purpose: `allow_inf_nan=False` on
     the three request models is the boundary, and
     `BottleTrackingService._as_decimal` is the SSOT backstop every non-HTTP
     caller passes through.
  4. `reconcile_balance` MUTATES on its own "nothing to do" path — it
     `get_or_create_balance`s, so checking a place that never moved a bottle
     creates a 0.00 row and grows the balances list.
  5. A fine's FROZEN scope outlives the balance row it writes to: paying a fine
     issued before its address joined a place creates an unreachable
     address-scoped row and the customer's real place never moves.
  6. The mirror of 5 for a DISSOLVED place: paying resurrects an orphaned
     group-scoped balance row for a memberless group.
  7. Pagination is unclamped and unvalidated on every paginated admin bottle
     route (`page=0` -> negative OFFSET). Only observable on real Postgres.
  8. A whitespace-only `notes` is an accepted justification for an unbounded
     admin balance write.
  9. The third face of 5/6, and the one that shows the mechanism cleanly:
     WAIVING such a fine writes a quantity-ZERO ledger row — no bottle moves at
     all — and still materialises a `bottle_balances` row in the frozen,
     unreachable scope. It is `get_or_create_balance` being a CREATE that is
     wrong, not the quantity.

CONSERVATION IS ASSERTED ON BOTH AXES, DELIBERATELY. `_assert_conserved` is a
GLOBAL oracle (Σ balances vs Σ coupled ledger quantities) and defects 5 and 6
PASS it: they move bottles into a scope nothing resolves to, which conserves the
global total while destroying the attribution. Those two companions therefore
assert the global oracle's silence AND the per-scope truth, so the blind spot is
documented in the suite rather than discovered in production.

TEST-INFRASTRUCTURE NOTE. The default backend is in-memory SQLite with FOREIGN
KEYS OFF and `with_for_update()` as a NO-OP, so nothing in the SQLite half of
this file proves FK integrity or locking. CHECK constraints ARE honoured there.
The `TestOnRealPostgres` block at the bottom uses `pg_app` / `pg_db` for exactly
the two things SQLite structurally cannot see: negative OFFSET/LIMIT and the
NOT NULL FK on `bottle_ledger.user_id`.
"""

import itertools
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import text

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import (
    BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
    BottleTrackingService,
)
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleFineStatus,
    BottleLedgerEventType,
    EntitySubtype,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)

pytestmark = pytest.mark.integration

API = "/api/v1/admin"
LAT, LNG = 41.3111, 69.2797

# Distinct-per-call phone/email suffixes. `users.phone` is UNIQUE and the shared
# conftest fixtures already own +99890123456{7,8,9} and +998901234570, so every
# identity this module mints lives in its own +9989000xxxxx block.
_SEQ = itertools.count(1)


# --------------------------------------------------------------------------- #
# Identities
# --------------------------------------------------------------------------- #


def _user(
    *,
    role=UserRole.CUSTOMER,
    user_type=UserType.INDIVIDUAL,
    status=UserStatus.ACTIVE,
    first_name="Aziz",
    last_name="Karimov",
    phone=None,
    entity_subtype=None,
):
    n = next(_SEQ)
    user = User(
        email=f"place-api-{n}@example.com",
        phone=phone or f"+99890000{n:05d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=first_name,
        last_name=last_name,
        user_type=user_type,
        entity_subtype=entity_subtype,
        role=role,
        status=status,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def _address(user, *, title="work", full_address="1 Office St, Tashkent"):
    addr = UserAddress(
        user_id=user.id,
        title=title,
        full_address=full_address,
        street_address="1 Office St",
        city="Tashkent",
        latitude=LAT,
        longitude=LNG,
    )
    _db.session.add(addr)
    _db.session.commit()
    return addr


def _headers(app, user, *, with_role_claim=False):
    """A real access token for `user`.

    `with_role_claim` mints the `role` claim that `manager_or_higher_required`
    (the admin address-DELETE route, and only that route on this axis) reads off
    the JWT — the rest of the surface uses `validate_admin_action`, which reads
    the DB row.
    """
    with app.app_context():
        token = create_access_token(
            identity=str(user.id),
            additional_claims={"role": user.role.value} if with_role_claim else None,
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _admin(app):
    user = _user(role=UserRole.ADMIN, user_type=UserType.STAFF, first_name="Root", last_name="Admin")
    return user, _headers(app, user)


def _operator(app):
    user = _user(role=UserRole.OPERATOR, user_type=UserType.STAFF, first_name="Ops", last_name="Person")
    return user, _headers(app, user)


def _manager(app):
    user = _user(role=UserRole.MANAGER, user_type=UserType.STAFF, first_name="Mid", last_name="Manager")
    return user, _headers(app, user)


# --------------------------------------------------------------------------- #
# Real write paths
# --------------------------------------------------------------------------- #


def _order(user):
    order = Order(user_id=user.id, status=OrderStatus.DELIVERED, total_amount=Decimal("50000.00"))
    _db.session.add(order)
    _db.session.commit()
    return order


def _deliver(user, address, quantity, *, actor=None):
    """A real DELIVERY ledger row against a real Order (never a hand-built row)."""
    order = _order(user)
    entry = BottleTrackingService().record_bottles_delivered(
        order_id=order.id,
        user_id=user.id,
        address_id=address.id,
        quantity=Decimal(str(quantity)),
        actor_user_id=(actor or user).id,
    )
    _db.session.commit()
    return entry


def _return(user, address, quantity, *, actor=None):
    order = _order(user)
    entry = BottleTrackingService().record_bottles_returned(
        user_id=user.id,
        address_id=address.id,
        quantity=Decimal(str(quantity)),
        order_id=order.id,
        delivery_id=None,
        actor_user_id=(actor or user).id,
    )
    _db.session.commit()
    return entry


def _adjust(user, address, delta, *, actor, notes="stock count"):
    return BottleTrackingService().admin_adjust_balance(
        user_id=user.id if user is not None else None,
        address_id=address.id,
        adjustment=Decimal(str(delta)),
        actor_user_id=actor.id,
        notes=notes,
    )


def _group(addresses, *, admin, reason="same office", label=None, **review):
    group = CustomerLinkService().create_place_group(
        [a.id for a in addresses], acting_admin_id=admin.id, reason=reason, label=label, **review
    )
    for a in addresses:
        _db.session.refresh(a)
    return group


def _manufacture_drift(address, stored):
    """Reproduce the dev address-24 shape: a stored figure the ledger cannot explain.

    THE ROW ITSELF IS THE SUBJECT here. There is no production writer that moves
    a balance without a ledger row except `reconcile_balance` (which moves it the
    other way), so drift is manufactured by seeding through the real
    `set_initial_balance` path and then DELETING the ledger row it wrote. The
    balance row was created by production code; only the ledger row is removed.
    """
    admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)
    entry = BottleTrackingService().set_initial_balance(
        user_id=None, address_id=address.id, quantity=Decimal(str(stored)), actor_user_id=admin.id
    )
    _db.session.query(BottleLedger).filter(BottleLedger.id == entry.id).delete()
    _db.session.commit()
    return admin


# --------------------------------------------------------------------------- #
# Oracles — always assert the PAIR
# --------------------------------------------------------------------------- #


def _place_balance(address_id):
    return BottleTrackingService.get_place_balance(address_id)


def _ledger_sum(scope):
    rows = BottleLedger.query.filter(*scope.ledger_filter()).all()
    return sum((Decimal(str(r.quantity or 0)) for r in rows), Decimal("0.00"))


def _global_balance_total():
    return sum((Decimal(str(b.balance or 0)) for b in BottleBalance.query.all()), Decimal("0.00"))


def _global_ledger_total():
    return sum((Decimal(str(e.quantity or 0)) for e in BottleLedger.query.all()), Decimal("0.00"))


def _is_decoupled(entry):
    key = entry.idempotency_key or ""
    return key.startswith(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)


def _conservation_probe():
    """Snapshot (Σ balances, Σ ledger, ledger ids) for a before/after PAIR."""
    return (
        _global_balance_total(),
        _global_ledger_total(),
        {e.id for e in BottleLedger.query.all()},
    )


def _assert_conserved(before):
    """Σ balances moved by exactly the COUPLED quantities appended since `before`.

    Asserting only "the place is 5 now" passes for a bug that also minted 2
    somewhere else, so this is always the pair.
    """
    bal_before, led_before, ids_before = before
    new = [e for e in BottleLedger.query.all() if e.id not in ids_before]
    coupled = sum((Decimal(str(e.quantity or 0)) for e in new if not _is_decoupled(e)), Decimal("0.00"))
    decoupled = sum((Decimal(str(e.quantity or 0)) for e in new if _is_decoupled(e)), Decimal("0.00"))
    assert _global_balance_total() - bal_before == coupled, (
        "Σ bottle_balances moved by something other than the coupled ledger quantities"
    )
    assert _global_ledger_total() - led_before == coupled + decoupled
    return new


# --------------------------------------------------------------------------- #
# The route inventory the permission matrix is swept over
# --------------------------------------------------------------------------- #


def _bottle_read_routes(*, address_id, user_id, session_id):
    return [
        ("get", f"{API}/bottles/dashboard", None),
        ("get", f"{API}/bottles/balances", None),
        ("get", f"{API}/bottles/balances/{user_id}", None),
        ("get", f"{API}/bottles/ledger", None),
        ("get", f"{API}/bottles/ledger/{address_id}", None),
        ("get", f"{API}/bottles/ledger/cluster/{user_id}", None),
        ("get", f"{API}/bottles/fines", None),
        ("get", f"{API}/bottles/sessions", None),
        ("get", f"{API}/bottles/sessions/{session_id}", None),
        ("get", f"{API}/bottles/transfers", None),
    ]


def _bottle_write_routes(*, address_id, user_id, session_id, fine_id, transfer_id):
    return [
        ("post", f"{API}/bottles/adjustment", {"address_id": address_id, "adjustment": 1, "notes": "x"}),
        ("post", f"{API}/bottles/initial-balance", {"address_id": address_id, "quantity": 1}),
        ("post", f"{API}/bottles/fines", {"address_id": address_id, "quantity": 1, "fine_amount": 1000}),
        ("put", f"{API}/bottles/fines/{fine_id}", {"action": "waive"}),
        ("post", f"{API}/bottles/reconcile/{address_id}", None),
        ("post", f"{API}/bottles/sessions/{session_id}/force-close", {"reason": "abandoned"}),
        ("post", f"{API}/bottles/transfers/{transfer_id}/resolve",
         {"resolved_quantity": 1, "resolution_notes": "x"}),
    ]


def _place_read_routes(*, group_id, address_id, other_address_id, user_id):
    return [
        ("get", f"{API}/place-groups/merge-preview?address_ids={address_id},{other_address_id}", None),
        ("get", f"{API}/place-groups/{group_id}", None),
        ("get", f"{API}/addresses/search?q=Karimov", None),
        ("get", f"{API}/users/{user_id}/place-group-suggestions", None),
    ]


def _place_write_routes(*, group_id, address_id, other_address_id, canonical_id=1):
    return [
        ("post", f"{API}/place-groups",
         {"addressIds": [address_id, other_address_id], "reason": "r"}),
        ("post", f"{API}/place-groups/{group_id}/addresses",
         {"addressIds": [other_address_id], "reason": "r"}),
        ("delete", f"{API}/place-groups/{group_id}/addresses/{address_id}", {"reason": "r"}),
        ("post", f"{API}/place-group-suggestions/dismiss",
         {"addressIdA": address_id, "addressIdB": other_address_id, "reason": "r"}),
        ("post", f"{API}/canonical-customers/{canonical_id}/address-groups",
         {"addressIds": [address_id, other_address_id]}),
    ]


def _call(client, method, url, payload, headers=None):
    fn = getattr(client, method)
    kwargs = {}
    if headers is not None:
        kwargs["headers"] = headers
    if payload is not None:
        kwargs["json"] = payload
    return fn(url, **kwargs)


# --------------------------------------------------------------------------- #
# A world every permission test can point at
# --------------------------------------------------------------------------- #


@pytest.fixture
def world(app, db):
    """One shared place, one solo place, a fine, a session, a real balance.

    Built entirely through production write paths so a route that returns 200
    has something real to leak, and a route that must 403 has something real to
    refuse.
    """
    admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF, first_name="Seed", last_name="Admin")
    coworker_a = _user(first_name="Aziz", last_name="Karimov", phone="+998900000568")
    coworker_b = _user(first_name="Co", last_name="Worker", phone="+998900000570")
    stranger = _user(first_name="Not", last_name="Related")

    a1 = _address(coworker_a, title="work")
    a2 = _address(coworker_b, title="work")
    solo = _address(stranger, title="home", full_address="9 Home St, Tashkent")

    group = _group([a1, a2], admin=admin, label="Acme office")
    _deliver(coworker_a, a1, 6, actor=admin)
    _deliver(coworker_b, a2, 5, actor=admin)
    _return(coworker_b, a2, 4, actor=admin)
    _deliver(stranger, solo, 3, actor=admin)

    fine = BottleTrackingService().issue_fine(
        user_id=None, address_id=a2.id, quantity=Decimal("2"),
        fine_amount=Decimal("50000"), actor_user_id=admin.id,
    )
    driver = _user(role=UserRole.DELIVERY_DRIVER, user_type=UserType.STAFF)
    session = BottleTrackingService().open_bottle_session(driver.id, 40, actor_user_id=admin.id)
    _db.session.commit()

    return {
        "admin": admin,
        "a": coworker_a,
        "b": coworker_b,
        "stranger": stranger,
        "a1": a1,
        "a2": a2,
        "solo": solo,
        "group": group,
        "fine": fine,
        "session": session,
        "driver": driver,
    }


# =========================================================================== #
# 0. THE ORACLE MUST BE ABLE TO SEE A VIOLATION
# =========================================================================== #


class TestTheConservationOracleIsFalsifiable:
    """A conservation helper that silently passes is worse than none: it
    advertises coverage that does not exist. Every `_assert_conserved` call in
    this file is worthless unless this test is red-capable, so it runs the ONE
    production writer that provably moves a balance with no ledger row at all —
    `reconcile_balance`, still exposed at `POST /admin/bottles/reconcile/<id>`
    and deliberately never called by the place lifecycle — and asserts the
    helper REPORTS the violation.
    """

    def test_the_helper_catches_a_balance_move_with_no_ledger_row(self, app, db):
        _admin_user, headers = _admin(app)
        drifted = _address(_user(), title="depot")
        _manufacture_drift(drifted, 20)
        assert _place_balance(drifted.id) == Decimal("20.00")

        before = _conservation_probe()
        response = app.test_client().post(f"{API}/bottles/reconcile/{drifted.id}",
                                         headers=headers)
        assert response.status_code == 200, response.get_json()
        _db.session.expire_all()
        assert _place_balance(drifted.id) == Decimal("0.00")     # 20 destroyed, no row

        with pytest.raises(AssertionError, match="other than the coupled ledger quantities"):
            _assert_conserved(before)

    def test_the_helper_does_not_count_a_decoupled_backfill_as_coupled(self, app, db):
        """The conservation split is only checkable because decoupled rows are
        identifiable BY THEIR KEY. If `merge_backfill:` ever stopped being
        recognised, this file's every conservation assertion would fail on any
        reviewed merge — and, worse, an unkeyed decoupled write would be counted
        as coupled and pass a pin it violates.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        x_owner = _user()
        x = _address(x_owner, title="x")
        y = _address(_user(), title="y")
        _deliver(x_owner, x, 6, actor=admin)
        _manufacture_drift(y, 20)

        preview = fresh.get(f"{API}/place-groups/merge-preview?address_ids={x.id},{y.id}",
                            headers=headers)
        entry_ids = preview.get_json()["data"]["entry_ids"]
        before = _conservation_probe()

        created = fresh.post(f"{API}/place-groups",
                             json={"addressIds": [x.id, y.id], "reason": "same office",
                                   "previewEntryIds": entry_ids, "resultingBalance": 10},
                             headers=headers)
        assert created.status_code == 201, created.get_json()

        _db.session.expire_all()
        appended = _assert_conserved(before)        # must NOT raise
        keys = {e.idempotency_key for e in appended}
        assert any(k and k.startswith("merge_backfill:") for k in keys), keys
        assert any(k and k.startswith("merge_correction:") for k in keys), keys
        assert [_is_decoupled(e) for e in appended].count(True) == 1


# =========================================================================== #
# 1. AUTHENTICATION — every route, no JWT at all
# =========================================================================== #


class TestUnauthenticated:
    """`handle_api_exception` is the OUTERMOST decorator, wrapping `jwt_required`.

    A fresh `app.test_client()` is used deliberately: the session-scoped
    `client` fixture is known to leak JWT cookies into 401 tests on this repo.

    UPDATED 2026-08-03. This class used to assert the `ExceptionMapper`
    envelope (`{'error': 'UNAUTHORIZED', 'status_code': 401, …}`) and its
    docstring claimed that reordering the stack would make
    `NoAuthorizationError` "become a bare 500". BOTH were wrong-ish, and the
    real defect was the opposite one: `handle_api_exception`'s blanket
    `except Exception` swallowed `ExpiredSignatureError` — which is NOT in
    `ExceptionMapper.EXCEPTION_MAPPING` — and turned every EXPIRED token on
    these 84 wrapped routes into a 500 with a CRITICAL log.

    `business_app/utils/error_handlers.py` now re-raises `JWTExtendedException`
    / `PyJWTError` so Flask routes them to the app's own JWT loaders
    (`setup_jwt_handlers`, `business_app/__init__.py`). Consequence, which is
    the point of the fix: the bottle family now answers with the SAME loader
    envelope as every other JWT-protected route in the API, including the
    place-group family below. The 84 wrapped routes were the outliers.

    What must never change, and is what this class actually guards: 401 on
    every route, never a 500, never a 200.
    """

    def test_every_bottle_route_401s_with_the_jwt_loader_envelope_and_never_500s(
        self, app, world
    ):
        fresh = app.test_client()
        routes = _bottle_read_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id
        ) + _bottle_write_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id,
            fine_id=world["fine"].id, transfer_id=1,
        )
        assert len(routes) == 17, "the bottle family has 17 routes; update this inventory"

        for method, url, payload in routes:
            resp = _call(fresh, method, url, payload)
            assert resp.status_code == 401, f"{method.upper()} {url} -> {resp.status_code}"
            body = resp.get_json()
            # flask-jwt-extended's `unauthorized_loader`, not `ExceptionMapper`.
            assert body.get("error") == "Authorization Required", (
                f"{method.upper()} {url} -> {body}"
            )
            assert body.get("message") == "Request does not contain an access token."

    def test_every_place_group_route_401s_and_never_500s(self, app, world):
        fresh = app.test_client()
        routes = _place_read_routes(
            group_id=world["group"].id, address_id=world["a1"].id,
            other_address_id=world["solo"].id, user_id=world["a"].id,
        ) + _place_write_routes(
            group_id=world["group"].id, address_id=world["a1"].id, other_address_id=world["solo"].id,
        )
        assert len(routes) == 9, "the place-group family has 9 routes; update this inventory"

        for method, url, payload in routes:
            resp = _call(fresh, method, url, payload)
            assert resp.status_code == 401, f"{method.upper()} {url} -> {resp.status_code}"

    def test_an_unauthenticated_write_leaves_the_place_untouched(self, app, world):
        fresh = app.test_client()
        before = _place_balance(world["a1"].id)
        rows = BottleLedger.query.count()

        fresh.post(f"{API}/bottles/adjustment",
                   json={"address_id": world["a1"].id, "adjustment": 99, "notes": "x"})

        assert _place_balance(world["a1"].id) == before
        assert BottleLedger.query.count() == rows


# =========================================================================== #
# 2. A CUSTOMER AND A DRIVER REACH NOTHING
# =========================================================================== #


class TestNonAdminIdentities:
    """`validate_admin_action`'s role gate is a list membership test against
    ADMIN/MANAGER/OPERATOR. Adding DELIVERY_DRIVER (or any new staff role) to
    that list silently opens all 17 bottle routes at once — and it is the only
    thing stopping a customer reading another place's ledger through
    `/bottles/ledger/<any address id>`.
    """

    @pytest.mark.parametrize(
        "role,user_type",
        [(UserRole.CUSTOMER, UserType.INDIVIDUAL), (UserRole.DELIVERY_DRIVER, UserType.STAFF)],
    )
    def test_every_bottle_route_403s(self, app, world, role, user_type):
        actor = _user(role=role, user_type=user_type)
        headers = _headers(app, actor)
        fresh = app.test_client()
        routes = _bottle_read_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id
        ) + _bottle_write_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id,
            fine_id=world["fine"].id, transfer_id=1,
        )

        for method, url, payload in routes:
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"
            body = resp.get_json()
            assert body["message"] == "Administrative access required", f"{url} -> {body}"

    @pytest.mark.parametrize(
        "role,user_type",
        [(UserRole.CUSTOMER, UserType.INDIVIDUAL), (UserRole.DELIVERY_DRIVER, UserType.STAFF)],
    )
    def test_every_place_group_route_403s(self, app, world, role, user_type):
        actor = _user(role=role, user_type=user_type)
        headers = _headers(app, actor)
        fresh = app.test_client()
        routes = _place_read_routes(
            group_id=world["group"].id, address_id=world["a1"].id,
            other_address_id=world["solo"].id, user_id=world["a"].id,
        ) + _place_write_routes(
            group_id=world["group"].id, address_id=world["a1"].id, other_address_id=world["solo"].id,
        )
        for method, url, payload in routes:
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"

    def test_no_403_body_leaks_a_balance_a_place_label_or_a_member_name(self, app, world):
        """A refusal must not answer the question it refused.

        The secrets are proven REAL first — an admin's 200 bodies contain every
        one of them — so this cannot pass by asserting the absence of strings
        that never appear anywhere.
        """
        actor = _user(role=UserRole.CUSTOMER)
        customer_headers = _headers(app, actor)
        _admin_user, admin_headers = _admin(app)
        fresh = app.test_client()
        secrets = ("Acme office", "Karimov", "50000", '"balance"', "place_label")
        routes = _bottle_read_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id
        )

        admin_corpus = "".join(
            _call(fresh, method, url, payload, admin_headers).get_data(as_text=True)
            for method, url, payload in routes
        )
        for secret in secrets:
            assert secret in admin_corpus, f"{secret!r} is not real — this test would be vacuous"

        for method, url, payload in routes:
            raw = _call(fresh, method, url, payload, customer_headers).get_data(as_text=True)
            for secret in secrets:
                assert secret not in raw, f"{url} leaked {secret!r}"


# =========================================================================== #
# 3. OPERATOR — the read tier, and nothing else, anywhere
# =========================================================================== #


class TestOperatorTier:
    """OPERATOR's permission list is `['view_orders','update_orders','view_products']`.

    Every bottle READ route asks for `['view_orders','manage_orders']` (any-of)
    and every bottle WRITE route asks for `['manage_orders']`. One typo —
    writing `['manage_orders']` as a read requirement, or adding
    `'manage_orders'` to the OPERATOR list — flips a whole tier, and
    `'update_orders'` looks close enough to `'manage_orders'` that a reviewer
    skims past it.
    """

    def test_operator_reads_every_bottle_screen(self, app, world):
        """200 alone would also be satisfied by a route that answered an empty
        body to a non-admin, so the operator's payloads are compared against the
        ADMIN's byte for byte: the read tier is a genuinely full read.
        """
        _op, op_headers = _operator(app)
        _admin_user, admin_headers = _admin(app)
        fresh = app.test_client()
        for method, url, payload in _bottle_read_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id
        ):
            resp = _call(fresh, method, url, payload, op_headers)
            assert resp.status_code == 200, f"{method.upper()} {url} -> {resp.status_code}"
            reference = _call(fresh, method, url, payload, admin_headers)
            assert reference.status_code == 200, url
            assert resp.get_json()["data"] == reference.get_json()["data"], (
                f"{url}: the operator's read differs from the admin's"
            )
        # ...and there really was something to read.
        balances = fresh.get(f"{API}/bottles/balances", headers=op_headers).get_json()["data"]
        assert balances["total"] >= 2 and balances["items"]

    def test_operator_writes_nothing_and_changes_nothing(self, app, world):
        _op, headers = _operator(app)
        fresh = app.test_client()
        before = _conservation_probe()
        balance_before = _place_balance(world["a1"].id)

        for method, url, payload in _bottle_write_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id,
            fine_id=world["fine"].id, transfer_id=1,
        ):
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"
            assert resp.get_json()["message"] == "Insufficient permissions for this action"

        assert _place_balance(world["a1"].id) == balance_before
        assert _assert_conserved(before) == []

    def test_operator_is_locked_out_of_the_entire_place_group_family(self, app, world):
        """An operator on the bottle screen sees shared-place rows and their
        member names but cannot open the place drawer — a half-visible feature.
        Any 'let operators see the place detail' change must be a deliberate
        403 -> 200 flip on this test, never a silent one.
        """
        _op, headers = _operator(app)
        fresh = app.test_client()
        routes = _place_read_routes(
            group_id=world["group"].id, address_id=world["a1"].id,
            other_address_id=world["solo"].id, user_id=world["a"].id,
        ) + _place_write_routes(
            group_id=world["group"].id, address_id=world["a1"].id, other_address_id=world["solo"].id,
        )
        for method, url, payload in routes:
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"


# =========================================================================== #
# 4. MANAGER — may move bottles, may NOT form a place
# =========================================================================== #


class TestManagerTier:
    """The asymmetry nobody has written down.

    The bottle family uses `view_orders`/`manage_orders`; the place-group family
    uses `view_users`/`manage_users`. MANAGER holds `view_users` and
    `manage_orders` but NOT `manage_users`, so a manager can adjust a place's
    bottle balance and run the destructive Reconcile, and cannot form a place.
    If someone 'harmonises' the codes, every MANAGER instantly gains place
    surgery — this test is where that shows up.
    """

    def test_manager_can_read_and_write_every_bottle_route(self, app, world):
        _mgr, headers = _manager(app)
        fresh = app.test_client()
        for method, url, payload in _bottle_read_routes(
            address_id=world["a1"].id, user_id=world["a"].id, session_id=world["session"].id
        ):
            assert _call(fresh, method, url, payload, headers).status_code == 200, url

        # A 200 is not proof a write LANDED; each one is re-read from the place.
        before = _place_balance(world["a1"].id)
        fines_before = BottleFine.query.count()
        adjust = fresh.post(f"{API}/bottles/adjustment",
                            json={"address_id": world["a1"].id, "adjustment": 1, "notes": "manager"},
                            headers=headers)
        assert adjust.status_code == 200, adjust.get_json()
        _db.session.expire_all()
        assert _place_balance(world["a1"].id) == before + Decimal("1")
        assert adjust.get_json()["data"]["actor_user_id"] == _mgr.id

        fine = fresh.post(f"{API}/bottles/fines",
                          json={"address_id": world["a1"].id, "quantity": 1, "fine_amount": 1000},
                          headers=headers)
        assert fine.status_code == 200, fine.get_json()
        assert BottleFine.query.count() == fines_before + 1
        assert BottleFine.query.get(fine.get_json()["data"]["id"]).issued_by == _mgr.id

        waive = fresh.put(f"{API}/bottles/fines/{world['fine'].id}",
                          json={"action": "waive"}, headers=headers)
        assert waive.status_code == 200, waive.get_json()
        _db.session.expire_all()
        waived = BottleFine.query.get(world["fine"].id)
        assert waived.status is BottleFineStatus.WAIVED and waived.waived_by == _mgr.id

    def test_manager_can_reach_the_destructive_reconcile(self, app, db, world):
        """`reconcile_balance` assigns `balance = ledger_sum`, writes NO ledger
        entry, and only logs. Plan C never calls it, yet it is exposed to every
        MANAGER via `manage_orders`. This test is the tripwire that says out
        loud who can destroy a hand-entered figure with one click.
        """
        _mgr, headers = _manager(app)
        fresh = app.test_client()
        drift_owner = _user()
        drifted = _address(drift_owner, title="warehouse")
        _manufacture_drift(drifted, 20)
        assert _place_balance(drifted.id) == Decimal("20.00")
        assert _ledger_sum(BottleScope.for_address(drifted.id)) == Decimal("0.00")
        ledger_rows_before = BottleLedger.query.count()

        resp = fresh.post(f"{API}/bottles/reconcile/{drifted.id}", headers=headers)

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["corrected"] is True
        assert data["discrepancy"] == 20.0
        assert data["previous_balance"] == 20.0
        assert data["recalculated_balance"] == 0.0
        # The pair: the stored figure is gone AND no ledger row explains it.
        assert _place_balance(drifted.id) == Decimal("0.00")
        assert BottleLedger.query.count() == ledger_rows_before

    def test_manager_can_preview_a_merge_but_cannot_commit_any_place_edit(self, app, world):
        _mgr, headers = _manager(app)
        fresh = app.test_client()

        preview = fresh.get(
            f"{API}/place-groups/merge-preview?address_ids={world['solo'].id}",
            headers=headers,
        )
        assert preview.status_code == 200, preview.get_json()
        detail = fresh.get(f"{API}/place-groups/{world['group'].id}", headers=headers)
        assert detail.status_code == 200, detail.get_json()
        assert fresh.get(f"{API}/addresses/search?q=Karimov", headers=headers).status_code == 200

        members_before = CustomerLinkService().get_place_group_address_ids(world["group"].id)
        for method, url, payload in _place_write_routes(
            group_id=world["group"].id, address_id=world["a1"].id, other_address_id=world["solo"].id,
        ):
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"
        assert CustomerLinkService().get_place_group_address_ids(world["group"].id) == members_before

    def test_the_admin_address_delete_route_uses_a_DIFFERENT_decorator(self, app, db):
        """`DELETE /admin/users/<u>/addresses/<a>` is guarded by
        `manager_or_higher_required`, not `validate_admin_action` — two
        permission mechanisms on adjacent routes is how one of them gets
        forgotten in a permissions refactor. MANAGER passes, OPERATOR does not.
        """
        owner = _user()
        keep = _address(owner, title="home")
        doomed = _address(owner, title="spare")
        operator, _ = _operator(app)
        manager, _ = _manager(app)
        fresh = app.test_client()

        refused = fresh.delete(
            f"{API}/users/{owner.id}/addresses/{doomed.id}",
            headers=_headers(app, operator, with_role_claim=True),
        )
        assert refused.status_code == 403
        assert UserAddress.query.get(doomed.id) is not None

        allowed = fresh.delete(
            f"{API}/users/{owner.id}/addresses/{doomed.id}",
            headers=_headers(app, manager, with_role_claim=True),
        )
        assert allowed.status_code == 200, allowed.get_json()
        assert UserAddress.query.get(doomed.id) is None
        assert UserAddress.query.get(keep.id) is not None


# =========================================================================== #
# 5. A LIVE JWT IS NOT A LIVE ACCOUNT
# =========================================================================== #


class TestRevokedIdentities:
    @pytest.mark.parametrize("status", [UserStatus.BANNED, UserStatus.INACTIVE,
                                       UserStatus.PENDING_VERIFICATION])
    def test_a_suspended_or_inactive_admin_is_403_on_both_families(self, app, world, status):
        """JWTs outlive a suspension, so the DB status check inside
        `validate_admin_action` is the ONLY thing that revokes a fired admin's
        live token. A refactor that moves it into a separate decorator applied
        to only some routes leaves a hole — so both families, read and write.
        """
        admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)
        headers = _headers(app, admin)          # minted BEFORE the status flip
        admin.status = status
        _db.session.commit()
        fresh = app.test_client()

        probes = [
            ("get", f"{API}/bottles/balances", None),
            ("post", f"{API}/bottles/adjustment",
             {"address_id": world["a1"].id, "adjustment": 1, "notes": "x"}),
            ("get", f"{API}/place-groups/{world['group'].id}", None),
            ("post", f"{API}/place-groups",
             {"addressIds": [world["a1"].id, world["solo"].id], "reason": "r"}),
        ]
        for method, url, payload in probes:
            resp = _call(fresh, method, url, payload, headers)
            assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code}"
        assert _call(fresh, "get", f"{API}/bottles/balances", None, headers).get_json()["message"] == (
            "Account suspended or inactive"
        )

    def test_a_token_for_a_deleted_user_is_401_not_500(self, app, world):
        """PINNED, and NOT what the route's own guard would say.

        `validate_admin_action` does `User.query.get(user_id)` and raises
        `ForbiddenError("User not found")` — but it never runs: the app's
        `user_lookup_loader` resolves the identity first and flask-jwt raises
        `UserLookupError`. So the decorator's own missing-user arm is
        UNREACHABLE through HTTP on this axis. What matters either way is that
        it is never a 500 and never a 200 — and that the identity is a STRING,
        so any change to identity typing turns this into 401 for EVERY request
        instead of just this one.

        UPDATED 2026-08-03. `UserLookupError` used to be caught by
        `handle_api_exception` and mapped to `{'error': 'UNAUTHORIZED'}` with the
        deleted user's id echoed in the message. It now reaches
        `user_lookup_error_loader`, which answers `{'error': 'User Not Found'}`
        with a GENERIC message. That is strictly better and the id assertion was
        dropped rather than reinstated: the old body leaked a real user id to an
        unauthenticated caller holding nothing but a stale token.
        """
        admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)
        headers = _headers(app, admin)
        deleted_id = admin.id
        _db.session.delete(admin)
        _db.session.commit()
        fresh = app.test_client()

        read = fresh.get(f"{API}/bottles/balances", headers=headers)
        assert read.status_code == 401, read.get_json()
        assert read.get_json()["error"] == "User Not Found"
        # The id must NOT be echoed back to an unauthenticated caller.
        assert str(deleted_id) not in read.get_json()["message"]
        write = fresh.post(f"{API}/bottles/adjustment",
                           json={"address_id": world["a1"].id, "adjustment": 1, "notes": "x"},
                           headers=headers)
        assert write.status_code == 401, write.get_json()
        # ...and the write really did not happen.
        assert _place_balance(world["a1"].id) == Decimal("7.00")

    def test_a_garbage_bearer_token_is_401_on_both_families_with_THE_SAME_body(
        self, app, world
    ):
        """FIXED 2026-08-03 — and the old version of this test is the changelog.

        WAS `…_with_DIFFERENT_bodies`. A malformed token raises
        `jwt.DecodeError`. On the BOTTLE family `handle_api_exception` caught it
        and emitted `{'error': 'UNAUTHORIZED', 'status_code': 401}`; the PLACE
        family has no such wrapper, so flask-jwt's own handler answered
        `{'error': 'Invalid Token'}` with no `status_code` key. Same HTTP code,
        two different bodies — a client branching on `error` saw two
        vocabularies for one condition. That divergence was the defect this test
        was written to pin, and pinning it EXACTLY per family (rather than as a
        loose `in (401, 422)`) is what made it visible.

        `error_handlers.py` now re-raises `JWTExtendedException` / `PyJWTError`,
        so both families reach `invalid_token_loader` and agree. Still pinned
        exactly per family, because the same divergence could reappear the
        moment anything re-wraps one family and not the other.
        """
        fresh = app.test_client()
        headers = {"Authorization": "Bearer not.a.jwt", "Content-Type": "application/json"}
        expected = {"error": "Invalid Token", "message": "The token is invalid."}

        bottle = fresh.get(f"{API}/bottles/balances", headers=headers)
        assert bottle.status_code == 401, bottle.get_data(as_text=True)
        assert bottle.get_json() == expected, bottle.get_json()

        place = fresh.get(f"{API}/place-groups/{world['group'].id}", headers=headers)
        assert place.status_code == 401, place.get_data(as_text=True)
        assert place.get_json() == expected


# =========================================================================== #
# 6. GET /admin/bottles/balances — the row the admin UI drives every action from
# =========================================================================== #


def _balances(client, headers, query=""):
    resp = client.get(f"{API}/bottles/balances{query}", headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


class TestBalancesListSerialization:
    def test_a_shared_place_row_carries_every_key_the_admin_ui_drives_actions_from(
        self, app, db, world
    ):
        """`placeAddressIdOf()` in BottleTracking.js is
        `record.address_id ?? record.representative_address_id`. A grouped row
        has `address_id` NULL by CHECK constraint, so drop or rename
        `representative_address_id` and EVERY row action on EVERY shared place
        posts `undefined` as the address id: the drawer opens on the right place
        and the write goes nowhere.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        data = _balances(fresh, headers)
        row = next(r for r in data["items"] if r["address_group_id"] == world["group"].id)

        assert row["is_shared_place"] is True
        assert row["address_id"] is None
        assert isinstance(row["balance"], (int, float)) and not isinstance(row["balance"], bool), (
            "a Decimal balance would render as the STRING \"7.00\" and break the sorter"
        )
        assert row["balance"] == 7.0
        assert row["place_label"] == "Acme office"
        assert row["member_names"] == ["Aziz Karimov", "Co Worker"]
        assert row["member_address_ids"] == sorted([world["a1"].id, world["a2"].id])
        assert row["representative_address_id"] == min(world["a1"].id, world["a2"].id)
        # `balance.address` is None for a grouped row, so the address keys are ABSENT.
        assert "address_title" not in row
        assert "full_address" not in row
        for key in ("id", "last_delivery_at", "last_return_at", "notes", "created_at", "updated_at"):
            assert key in row

    def test_the_serialized_keys_are_the_ones_the_component_actually_reads(self, app, db, world):
        """Pinned against `admin_ui/src/pages/BottleTracking.js` itself rather
        than a hand-copied list: this fails if the serializer drops a key the
        component reads, AND flags drift if the component stops reading one.

        The component side is matched as a `record.<key>` ACCESSOR, not as a bare
        substring — `"balance"` and `"address_id"` occur dozens of times in that
        file in unrelated contexts (form field names, i18n keys, drawer state),
        so a bare `in source` check would stay green after the component stopped
        reading the key entirely. `placeAddressIdOf` is asserted verbatim because
        it is the single rule EVERY row action on EVERY shared place goes
        through.
        """
        component = Path(__file__).resolve().parents[2] / "admin_ui/src/pages/BottleTracking.js"
        source = component.read_text()
        _admin_user, headers = _admin(app)
        row = next(
            r for r in _balances(app.test_client(), headers)["items"]
            if r["address_group_id"] == world["group"].id
        )
        assert "record.address_id ?? record.representative_address_id" in source, (
            "placeAddressIdOf's fallback is the whole reason a grouped row is actionable"
        )
        for key in (
            "place_label", "is_shared_place", "member_names", "member_address_ids",
            "representative_address_id", "address_id", "balance",
        ):
            assert f"record.{key}" in source, f"BottleTracking.js no longer reads record.{key}"
            assert key in row, f"the balances payload no longer carries {key}"

    def test_a_solo_place_row_carries_the_address_title_and_full_address(self, app, db, world):
        """The Address column renders
        `record.is_shared_place ? '—' : (full_address || address_title || '—')`,
        and `_scope_member_address_ids` returning [] for a solo row would lose
        the row its action target entirely.
        """
        _admin_user, headers = _admin(app)
        data = _balances(app.test_client(), headers)
        row = next(r for r in data["items"] if r["address_id"] == world["solo"].id)

        assert row["is_shared_place"] is False
        assert row["address_group_id"] is None
        assert row["representative_address_id"] == world["solo"].id
        assert row["member_address_ids"] == [world["solo"].id]
        assert row["member_names"] == ["Not Related"]
        assert row["address_title"] == "home"
        assert row["full_address"] == "9 Home St, Tashkent"
        assert row["place_label"] == "home"

    def test_place_label_falls_back_through_all_three_tiers(self, app, db):
        """`AddressGroup.label` and `UserAddress.title` are both nullable;
        `', '.join(...) or f'Place #{id}'` is the only thing between a null
        label and a blank cell on the admin table. The set/sorted dedup is easy
        to lose in a refactor, producing 'Work, Work'.
        """
        _admin_user, headers = _admin(app)
        admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)

        labelled = _group([_address(_user(), title="x"), _address(_user(), title="y")],
                          admin=admin, label="Named Place")
        _adjust(None, UserAddress.query.filter_by(address_group_id=labelled.id).first(),
                1, actor=admin)

        titled_a = _address(_user(), title="Work")
        titled_b = _address(_user(), title="Work")            # duplicate title on purpose
        titled_c = _address(_user(), title="Annex")
        titled = _group([titled_a, titled_b, titled_c], admin=admin, label=None)
        _adjust(None, titled_a, 2, actor=admin)

        blank_a = _address(_user(), title=None)
        blank_b = _address(_user(), title="")
        blank = _group([blank_a, blank_b], admin=admin, label=None)
        _adjust(None, blank_a, 3, actor=admin)

        rows = {r["address_group_id"]: r for r in _balances(app.test_client(), headers)["items"]}
        assert rows[labelled.id]["place_label"] == "Named Place"
        assert rows[titled.id]["place_label"] == "Annex, Work", "titles must be sorted and de-duplicated"
        assert rows[blank.id]["place_label"] == f"Place #{blank.id}"
        for row in rows.values():
            assert row["place_label"], "place_label must never be null or empty"

    def test_a_solo_place_with_no_title_falls_back_to_its_address_id(self, app, db):
        _admin_user, headers = _admin(app)
        owner = _user()
        addr = _address(owner, title=None)
        admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)
        _adjust(owner, addr, 4, actor=admin)

        row = next(r for r in _balances(app.test_client(), headers)["items"]
                   if r["address_id"] == addr.id)
        assert row["place_label"] == f"Address #{addr.id}"


class TestBalancesListFilters:
    def _four_places(self, admin):
        """Balances -3.00, 0.00, 0.01 and 7.00, every one from a real write."""
        negative_owner, zero_owner, cent_owner, seven_owner = (_user() for _ in range(4))
        negative = _address(negative_owner, title="neg")
        zero = _address(zero_owner, title="zero")
        cent = _address(cent_owner, title="cent")
        seven = _address(seven_owner, title="seven")

        _deliver(negative_owner, negative, 2, actor=admin)
        _return(negative_owner, negative, 5, actor=admin)        # over-returned -> -3
        _deliver(zero_owner, zero, 4, actor=admin)
        _return(zero_owner, zero, 4, actor=admin)                # nets to 0
        _adjust(cent_owner, cent, "0.01", actor=admin)
        _deliver(seven_owner, seven, 7, actor=admin)
        return {"negative": negative, "zero": zero, "cent": cent, "seven": seven}

    def test_min_balance_boundaries_including_zero_and_negative(self, app, db):
        """`if min_balance is not None` is the ONLY reason `min_balance=0` is
        not treated as "no filter" — a falsy check here would silently include
        the -3 place. Over-returned places are real: they are what makes
        `suggested_bottles_leaving`'s `max(0, place)` clamp necessary.
        """
        admin, headers = _admin(app)
        places = self._four_places(admin)
        fresh = app.test_client()
        ids = {v.id for v in places.values()}

        def matched(query):
            data = _balances(fresh, headers, query)
            return {r["address_id"] for r in data["items"] if r["address_id"] in ids}, data

        unfiltered, _ = matched("")
        assert unfiltered == ids

        at_zero, _ = matched("?min_balance=0")
        assert at_zero == {places["zero"].id, places["cent"].id, places["seven"].id}

        at_cent, _ = matched("?min_balance=0.01")
        assert at_cent == {places["cent"].id, places["seven"].id}

        at_negative, _ = matched("?min_balance=-3")
        assert at_negative == ids

        _above, data = matched("?min_balance=8")
        assert data["total"] == 0 and data["pages"] == 0 and data["items"] == []

    def test_user_id_filter_selects_a_shared_place_through_either_member(self, app, db, world):
        """A place is reachable only through the group arm. If the solo arm ever
        drops its `address_group_id IS NULL` conjunct it will also match the
        group row's NULL `address_id` and start returning unrelated places.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        via_a = _balances(fresh, headers, f"?user_id={world['a'].id}")
        via_b = _balances(fresh, headers, f"?user_id={world['b'].id}")

        assert len(via_a["items"]) == 1
        assert via_a == via_b, "both members must see the same single shared place row"
        assert via_a["items"][0]["address_group_id"] == world["group"].id
        assert via_a["items"][0]["representative_address_id"] == min(world["a1"].id, world["a2"].id)

    def test_user_id_filter_for_a_user_with_no_addresses_returns_empty_not_everything(
        self, app, db, world
    ):
        """`query.filter(or_(*clauses)) if clauses else query.filter(sa_false())`
        — drop the `sa_false()` fallback and `or_()` with no clauses is a no-op,
        so a query for ONE customer returns EVERY place in the system.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        addressless = _user()
        assert _balances(fresh, headers)["total"] >= 2      # there is something to over-return

        for query in (f"?user_id={addressless.id}", "?user_id=999999"):
            data = _balances(fresh, headers, query)
            assert data["items"] == [], query
            assert data["total"] == 0 and data["pages"] == 0, query

    def test_search_matches_any_member_by_first_name_last_name_and_phone(self, app, db, world):
        """The search join is `join(User, UserAddress.user_id == User.id)` — the
        multi-FK gotcha. A match on the NON-owning coworker must still surface
        the shared place, which is the whole point of a place having no owner.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        for query in ("Karimov", "Worker", "0000568", "0000570"):
            data = _balances(fresh, headers, f"?search={query}")
            assert [r["address_group_id"] for r in data["items"]] == [world["group"].id], query

        nothing = _balances(fresh, headers, "?search=zzz-no-match")
        assert nothing["total"] == 0 and nothing["items"] == []

    def test_pagination_first_last_and_past_the_end(self, app, db):
        """Ordering is `balance DESC` and this sweep uses 25 DISTINCT balances on
        purpose: with ties that ordering is NOT a total order, so two places at
        7.00 can swap between page requests — one shown twice, another never.
        Postgres and SQLite differ on tie ordering, so a tie-based version of
        this test would pass here and duplicate rows in production.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        expected = []
        for i in range(25):
            owner = _user()
            addr = _address(owner, title=f"p{i}")
            _deliver(owner, addr, 100 - i, actor=admin)
            expected.append(addr.id)

        first = _balances(fresh, headers, "?page=1&per_page=10")
        assert first["total"] == 25 and first["pages"] == 3 and len(first["items"]) == 10
        second = _balances(fresh, headers, "?page=2&per_page=10")
        third = _balances(fresh, headers, "?page=3&per_page=10")
        assert len(third["items"]) == 5
        past = _balances(fresh, headers, "?page=4&per_page=10")
        assert past["items"] == [] and past["total"] == 25 and past["pages"] == 3

        walked = [r["address_id"] for page in (first, second, third) for r in page["items"]]
        assert len(walked) == len(set(walked)) == 25, "a page boundary duplicated or skipped a place"
        assert walked == expected, "ordering must be balance DESC and stable across pages"

        one_page = _balances(fresh, headers, "?per_page=25&page=1")
        assert len(one_page["items"]) == 25 and one_page["pages"] == 1

    def test_non_numeric_pagination_and_filter_args_fall_back_to_the_defaults(self, app, db, world):
        """Flask's `type=int` swallows the conversion error and returns the
        default. `min_balance` uses `type=float`, where a garbage value is also
        dropped — but see `test_BUG_min_balance_NaN_*` for the value that is a
        VALID float.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        resp = fresh.get(
            f"{API}/bottles/balances?page=abc&per_page=xyz&min_balance=oops&user_id=nope",
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["page"] == 1 and data["per_page"] == 20
        assert data["total"] >= 2, "a garbage user_id must not become an empty filter"

        ledger = fresh.get(f"{API}/bottles/ledger?user_id=oops&address_id=oops", headers=headers)
        assert ledger.status_code == 200, ledger.get_json()
        assert ledger.get_json()["data"]["total"] >= 1

    def test_min_balance_NaN_passes_the_boundary_while_plain_garbage_does_not(
        self, app, db, world
    ):
        """PINNED, not asserted-as-correct — and the surprise is the ASYMMETRY.

        `min_balance=oops` is dropped by Flask's `type=float` and the route
        answers with NO filter. `min_balance=NaN` is a VALID Python float, so it
        is accepted and becomes `balance >= NaN` — a predicate that matches
        NOTHING (verified on this backend; Postgres agrees, every comparison
        against NaN being false there too). Two spellings of the same nonsense
        produce two opposite answers — everything, or nothing — and neither is
        an error.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        garbage = fresh.get(f"{API}/bottles/balances?min_balance=oops", headers=headers)
        assert garbage.status_code == 200, garbage.get_data(as_text=True)
        unfiltered_total = garbage.get_json()["data"]["total"]
        assert unfiltered_total >= 2

        nan = fresh.get(f"{API}/bottles/balances?min_balance=NaN", headers=headers)
        assert nan.status_code == 200, nan.get_data(as_text=True)
        # Whatever it returns, it is not a filter anybody asked for.
        # EXACT, not a two-way range: `balance >= NaN` selects NOTHING, so the
        # admin is told "no place holds bottles" while `unfiltered_total` places
        # do. A range that also accepted `unfiltered_total` would pass whether
        # the predicate filtered everything, nothing, or was dropped entirely.
        assert nan.get_json()["data"]["total"] == 0
        assert nan.get_json()["data"]["items"] == []
        assert unfiltered_total != 0, "the empty answer must be WRONG, not merely empty"


# =========================================================================== #
# 7. GET /admin/bottles/balances/<user_id> — the per-customer summary
# =========================================================================== #


class TestCustomerSummaryRoute:
    def test_a_linked_customer_at_a_shared_place_sees_places_once_and_no_scalar_total(
        self, app, db
    ):
        """`get_customer_scopes` de-duplicates by group_id; lose that and a
        person with two addresses in one office double-counts the office's
        bottles on the admin's screen. The route's own docstring warns that
        summing `addresses[].place_balance` is WRONG — asserted here as an
        inequality so a well-meaning contributor cannot "fix" it into a total.
        """
        admin, headers = _admin(app)
        alice = _user(first_name="Ali", last_name="One")
        bob = _user(first_name="Bob", last_name="Two")
        office_a = _address(alice, title="office")
        office_b = _address(bob, title="office")
        alice_second_office = _address(alice, title="office-annex")
        alice_home = _address(alice, title="home")

        group = _group([office_a, office_b, alice_second_office], admin=admin)
        _deliver(alice, office_a, 6, actor=admin)
        _deliver(bob, office_b, 5, actor=admin)
        _return(bob, office_b, 4, actor=admin)
        _deliver(alice, alice_home, 2, actor=admin)
        CustomerLinkService().link_accounts(alice.id, bob.id, actor_admin_id=admin.id, reason="same person")

        resp = app.test_client().get(f"{API}/bottles/balances/{alice.id}", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]

        assert {a["address_id"] for a in data["addresses"]} == {
            office_a.id, alice_second_office.id, alice_home.id
        }, "addresses[] lists only the addresses this customer OWNS"
        for row in data["addresses"]:
            if row["address_id"] in (office_a.id, alice_second_office.id):
                assert row["address_group_id"] == group.id and row["is_grouped"] is True
                assert row["place_balance"] == 7.0
            else:
                assert row["is_grouped"] is False and row["place_balance"] == 2.0

        # cluster_scopes: one row per DISTINCT place, never the office twice.
        assert len(data["cluster_scopes"]) == 2
        assert sum(s["balance"] for s in data["cluster_scopes"]) == 9.0
        assert sum(a["place_balance"] for a in data["addresses"]) == 16.0, (
            "the addresses[] sum double-counts the office — the docstring's own warning"
        )
        assert "total_balance" not in data and "total" not in data

        assert data["is_linked"] is True
        assert data["cluster_member_ids"] == sorted([alice.id, bob.id])

    def test_active_fine_count_and_amount_describe_the_same_two_statuses(self, app, db):
        """Two independent queries repeat the same status IN-list. Edit one and
        not the other and the admin sees '2 fines, 0 UZS'. The four amounts are
        distinct so a wrong subset cannot coincidentally match.
        """
        admin, headers = _admin(app)
        owner = _user()
        addr = _address(owner)
        svc = BottleTrackingService()
        amounts = {
            BottleFineStatus.PENDING: Decimal("11000"),
            BottleFineStatus.INVOICED: Decimal("22000"),
            BottleFineStatus.PAID: Decimal("44000"),
            BottleFineStatus.WAIVED: Decimal("88000"),
        }
        for status, amount in amounts.items():
            fine = svc.issue_fine(user_id=None, address_id=addr.id, quantity=Decimal("1"),
                                  fine_amount=amount, actor_user_id=admin.id)
            fine.status = status
            _db.session.commit()

        data = app.test_client().get(
            f"{API}/bottles/balances/{owner.id}", headers=headers
        ).get_json()["data"]

        assert data["active_fines_count"] == 2
        assert data["total_fine_amount"] == float(
            amounts[BottleFineStatus.PENDING] + amounts[BottleFineStatus.INVOICED]
        )

    def test_a_nonexistent_customer_id_answers_200_with_an_empty_payload(self, app, db, world):
        """PINNED AS CURRENT BEHAVIOUR, and deliberately contrasted with its
        neighbour. `get_cluster_user_ids` returns `[999999]` for a missing user,
        so both bottle routes answer 200-with-empty, while
        `/admin/users/999999/place-group-suggestions` 404s via `user_exists`.
        On a debt-collection screen a typo'd id reading as "owes no bottles" is
        the exact wrong answer — this test is where a decision to 404 lands.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        summary = fresh.get(f"{API}/bottles/balances/999999", headers=headers)
        assert summary.status_code == 200
        assert summary.get_json()["data"] == {
            "user_id": 999999,
            "addresses": [],
            "active_fines_count": 0,
            "total_fine_amount": 0.0,
            "is_linked": False,
            "cluster_member_ids": [999999],
            "cluster_scopes": [],
        }

        cluster = fresh.get(f"{API}/bottles/ledger/cluster/999999", headers=headers)
        assert cluster.status_code == 200
        assert cluster.get_json()["data"]["items"] == []

        neighbour = fresh.get(f"{API}/users/999999/place-group-suggestions", headers=headers)
        assert neighbour.status_code == 404, "the adjacent admin route disagrees — deliberately pinned"


# =========================================================================== #
# 8. THE LEDGER ROUTES — an attribution filter and a PLACE filter
# =========================================================================== #


class TestGlobalLedgerRoute:
    def test_user_id_is_an_ATTRIBUTION_filter_not_a_place_filter(self, app, db, world):
        """The docstring pins `user_id` as "what did this person move" while
        `address_id` is a PLACE filter. Unifying them — reasonable-looking,
        since both describe "this customer's bottles" — would make the person
        filter return the whole office's movements: a cross-customer disclosure
        inside the admin ledger.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        def ids(query):
            resp = fresh.get(f"{API}/bottles/ledger{query}", headers=headers)
            assert resp.status_code == 200, resp.get_json()
            return {e["id"] for e in resp.get_json()["data"]["items"]}

        place_scope = BottleScope.for_group(world["group"].id)
        all_place_rows = BottleLedger.query.filter(*place_scope.ledger_filter()).all()
        a_rows = {e.id for e in all_place_rows if e.user_id == world["a"].id}
        b_rows = {e.id for e in all_place_rows if e.user_id == world["b"].id}
        assert a_rows and b_rows and a_rows != b_rows

        assert ids(f"?user_id={world['a'].id}") == a_rows
        assert ids(f"?user_id={world['b'].id}") == b_rows

    def test_address_id_expands_to_the_WHOLE_place_from_either_member(self, app, db, world):
        """`resolve_scope` expands either member address to the group. Degrade
        this to a literal `BottleLedger.address_id == x` and the admin sees half
        a place's history depending on which member they clicked — with no error.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        via_a = fresh.get(f"{API}/bottles/ledger?address_id={world['a1'].id}", headers=headers)
        via_b = fresh.get(f"{API}/bottles/ledger?address_id={world['a2'].id}", headers=headers)
        assert via_a.status_code == via_b.status_code == 200
        set_a = {e["id"] for e in via_a.get_json()["data"]["items"]}
        set_b = {e["id"] for e in via_b.get_json()["data"]["items"]}

        expected = {
            e.id for e in BottleLedger.query.filter(
                *BottleScope.for_group(world["group"].id).ledger_filter()
            ).all()
        }
        assert set_a == set_b == expected
        assert via_a.get_json()["data"]["total"] == via_b.get_json()["data"]["total"] == len(expected)

    def test_a_missing_address_is_a_404_on_both_ledger_shapes(self, app, db, world):
        """`resolve_scope` deliberately RAISES rather than falling back to a
        singleton scope, because a balance keyed to a nonexistent address
        violates the FK on Postgres while passing silently in the FK-off suite.
        Reinstate the fallback and this route returns a confident empty ledger
        for an address that does not exist.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        for url in (f"{API}/bottles/ledger?address_id=999999", f"{API}/bottles/ledger/999999"):
            resp = fresh.get(url, headers=headers)
            assert resp.status_code == 404, f"{url} -> {resp.status_code} {resp.get_json()}"
            assert resp.get_json()["error"] == "NOT_FOUND"

    def test_an_unknown_event_type_is_a_400_and_every_real_one_round_trips(self, app, db, world):
        """`BottleLedgerEventType(event_type)` raises a bare `ValueError` inside
        the handler; only `handle_api_exception`'s ValueError -> 400 mapping
        keeps it out of 500 territory.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        bad = fresh.get(f"{API}/bottles/ledger?event_type=not_a_real_event", headers=headers)
        assert bad.status_code == 400, bad.get_json()
        assert bad.get_json()["error"] == "INVALID_VALUE"
        assert bad.get_json()["message"]

        for member in BottleLedgerEventType:
            ok = fresh.get(f"{API}/bottles/ledger?event_type={member.value}", headers=headers)
            assert ok.status_code == 200, f"{member.value} -> {ok.get_json()}"
            for item in ok.get_json()["data"]["items"]:
                assert item["event_type"] == member.value

    def test_an_unknown_fine_status_is_a_400_and_every_real_one_round_trips(self, app, db, world):
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        bad = fresh.get(f"{API}/bottles/fines?status=bogus", headers=headers)
        assert bad.status_code == 400, bad.get_json()
        assert bad.get_json()["error"] == "INVALID_VALUE"

        for member in BottleFineStatus:
            ok = fresh.get(f"{API}/bottles/fines?status={member.value}", headers=headers)
            assert ok.status_code == 200, f"{member.value} -> {ok.get_json()}"
            # A 200 alone would also pass if the filter were silently dropped.
            for item in ok.get_json()["data"]["items"]:
                assert item["status"] == member.value, (member.value, item)
        # ...and the statuses really do partition the fines, so the loop above
        # was not vacuously iterating over empty pages.
        every = fresh.get(f"{API}/bottles/fines?per_page=100", headers=headers)
        assert every.get_json()["data"]["total"] >= 1


class TestPlaceLedgerRoute:
    def test_the_place_ledger_is_identical_at_both_members_and_carries_balance_after(
        self, app, db, world
    ):
        """`balance_after` is a derived snapshot written at append time, and it
        is what the admin drawer renders as a running total. Asserting only the
        quantities would miss a non-monotonic snapshot column entirely.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        via_a = fresh.get(f"{API}/bottles/ledger/{world['a1'].id}", headers=headers)
        via_b = fresh.get(f"{API}/bottles/ledger/{world['a2'].id}", headers=headers)
        assert via_a.status_code == via_b.status_code == 200
        assert via_a.get_json()["data"] == via_b.get_json()["data"]

        items = via_a.get_json()["data"]["items"]
        movements = [i for i in items if i["event_type"] != BottleLedgerEventType.FINE_ISSUED.value]
        # Newest first (occurred_at DESC, id DESC) -> reverse for chronology.
        chronological = list(reversed(movements))
        assert [i["quantity"] for i in chronological] == [6.0, 5.0, -4.0]
        assert [i["balance_after"] for i in chronological] == [6.0, 11.0, 7.0]

        assert _place_balance(world["a1"].id) == Decimal("7.00")
        assert _ledger_sum(BottleScope.for_group(world["group"].id)) == Decimal("7.00")

    def test_a_solo_addresses_place_ledger_excludes_a_former_groups_rows(self, app, db):
        """`BottleScope.ledger_filter`'s ungrouped arm keeps
        `address_group_id IS NULL`; after a departure an address's rows stay
        stamped with the former group, and filtering on `address_id` alone would
        pull that whole place's history back into the departed address.
        """
        admin, headers = _admin(app)
        left_owner, stay_owner = _user(), _user()
        leaving = _address(left_owner, title="leaving")
        staying = _address(stay_owner, title="staying")
        third = _address(_user(), title="third")
        group = _group([leaving, staying, third], admin=admin)
        _deliver(left_owner, leaving, 6, actor=admin)

        CustomerLinkService().remove_address_from_group(
            leaving.id, acting_admin_id=admin.id, reason="moved out"
        )

        resp = app.test_client().get(f"{API}/bottles/ledger/{leaving.id}", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["items"] == [], (
            "the departed address's own scope is empty — its rows still carry the former group"
        )
        group_rows = BottleLedger.query.filter(BottleLedger.address_group_id == group.id).count()
        assert group_rows == 1


class TestClusterLedgerRoute:
    def test_the_cluster_ledger_spans_every_place_without_duplicating_one(self, app, db):
        """The clause is a set of group_ids OR a set of solo ids. A `union_all`
        refactor would duplicate rows for the member who owns TWO addresses in
        the same shared place — exactly the shape built here.
        """
        admin, headers = _admin(app)
        alice, bob = _user(first_name="Ali"), _user(first_name="Bob")
        solo = _address(alice, title="home")
        office_a = _address(alice, title="office")
        office_a2 = _address(alice, title="office-2")     # SECOND address in the same place
        office_b = _address(bob, title="office")
        group = _group([office_a, office_a2, office_b], admin=admin)

        _deliver(alice, solo, 1, actor=admin)
        _return(alice, solo, 1, actor=admin)              # 2 entries in the solo place
        _deliver(alice, office_a, 3, actor=admin)
        _deliver(bob, office_b, 4, actor=admin)
        _return(bob, office_b, 2, actor=admin)            # 3 entries in the shared place
        CustomerLinkService().link_accounts(alice.id, bob.id, actor_admin_id=admin.id, reason="same")

        fresh = app.test_client()
        for anchor in (alice, bob):
            resp = fresh.get(f"{API}/bottles/ledger/cluster/{anchor.id}?per_page=50", headers=headers)
            assert resp.status_code == 200, resp.get_json()
            items = resp.get_json()["data"]["items"]
            ids = [i["id"] for i in items]
            assert len(ids) == len(set(ids)) == 5, f"anchor {anchor.id} -> {ids}"
            assert resp.get_json()["data"]["total"] == 5
        assert group.id is not None


# =========================================================================== #
# 9. POST /admin/bottles/adjustment
# =========================================================================== #


class TestAdjustmentRoute:
    def test_no_user_id_derives_the_representative_members_owner_deterministically(
        self, app, db, world
    ):
        """The derivation is "lowest member address id, then its owner" and it
        must match `serialize_bottle_balance`'s `representative_address_id`
        exactly. Lose `_place_member_address_ids`' ORDER BY id ASC and two
        identical admin clicks attribute to two different coworkers.

        The NON-representative member's address is sent on purpose — that is
        what the UI does when it has no member to name.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        representative = min(world["a1"].id, world["a2"].id)
        expected_owner = UserAddress.query.get(representative).user_id
        target = world["a2"] if world["a2"].id != representative else world["a1"]
        before = _conservation_probe()
        balance_before = _place_balance(target.id)

        first = fresh.post(f"{API}/bottles/adjustment",
                           json={"address_id": target.id, "adjustment": 3, "notes": "stock count"},
                           headers=headers)
        assert first.status_code == 200, first.get_json()
        entry = first.get_json()["data"]
        assert entry["user_id"] == expected_owner
        assert entry["address_id"] == target.id
        assert entry["address_group_id"] == world["group"].id
        assert entry["event_type"] == BottleLedgerEventType.ADMIN_ADJUSTMENT.value
        assert entry["quantity"] == 3.0
        assert _place_balance(target.id) == balance_before + Decimal("3")

        second = fresh.post(f"{API}/bottles/adjustment",
                            json={"address_id": target.id, "adjustment": 3, "notes": "again"},
                            headers=headers)
        assert second.status_code == 200, second.get_json()
        assert second.get_json()["data"]["user_id"] == expected_owner, "derivation must be deterministic"

        appended = _assert_conserved(before)
        assert sum(Decimal(str(e.quantity)) for e in appended) == Decimal("6")

    def test_an_explicit_in_scope_user_id_is_honoured_not_overwritten(self, app, db, world):
        """The "derive when absent" branch must not become "always derive".
        Silently overwriting an explicitly named member destroys the caller's
        intent with no error — the worst kind of change, because nothing fails.
        """
        _admin_user, headers = _admin(app)
        representative = min(world["a1"].id, world["a2"].id)
        non_representative_owner = next(
            UserAddress.query.get(a).user_id
            for a in (world["a1"].id, world["a2"].id) if a != representative
        )
        before = _place_balance(world["a1"].id)

        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"user_id": non_representative_owner, "address_id": representative,
                  "adjustment": -2, "notes": "named member"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["user_id"] == non_representative_owner
        assert data["address_id"] == representative
        assert data["quantity"] == -2.0
        assert _place_balance(world["a1"].id) == before - Decimal("2")

    def test_notes_is_required_and_an_empty_string_is_refused(self, app, db, world):
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        before = _place_balance(world["a1"].id)
        rows = BottleLedger.query.count()

        omitted = fresh.post(f"{API}/bottles/adjustment",
                             json={"address_id": world["a1"].id, "adjustment": 1},
                             headers=headers)
        assert omitted.status_code == 400, omitted.get_json()
        assert any("notes" in e for e in omitted.get_json()["errors"])

        empty = fresh.post(f"{API}/bottles/adjustment",
                           json={"address_id": world["a1"].id, "adjustment": 1, "notes": ""},
                           headers=headers)
        assert empty.status_code == 400, empty.get_json()
        assert "Notes are required" in empty.get_json()["message"]

        assert _place_balance(world["a1"].id) == before
        assert BottleLedger.query.count() == rows

    def test_BUG_a_whitespace_only_note_is_not_a_note(self, app, db, world):
        """FIXED — the strict xfail is gone.

        WAS: `admin_adjust_balance` rejected only a FALSY notes, so a
        whitespace-only string was an accepted justification for an unbounded
        balance write. Every place-group route .strip()s its reason; this one
        did not.

        NOW the route strips `notes` before handing it over, so "   " collapses
        to "" and hits the service's existing "Notes are required" fence.
        """
        _admin_user, headers = _admin(app)
        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"address_id": world["a1"].id, "adjustment": 5, "notes": "   "},
            headers=headers,
        )
        assert resp.status_code == 400, "an unaccountable adjustment must be refused"

    @pytest.mark.parametrize(
        "delta,expected",
        [(0, "7.00"), ("0.5", "7.50"), ("-0.25", "6.75"), ("-7", "0.00"), ("-10", "-3.00")],
    )
    def test_zero_fractional_and_negative_adjustments_land_exactly(
        self, app, db, world, delta, expected
    ):
        """`format_bottle_quantity` exists because someone tried int()
        truncation before. `Numeric(12,2)` must survive the fraction, a zero
        must write a real (no-op) row, and an adjustment is deliberately
        unbounded downwards: a place CAN be negative.
        """
        _admin_user, headers = _admin(app)
        before = _conservation_probe()
        assert _place_balance(world["a1"].id) == Decimal("7.00")

        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"address_id": world["a1"].id, "adjustment": delta, "notes": "n"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert _place_balance(world["a1"].id) == Decimal(expected)
        assert resp.get_json()["data"]["balance_after"] == float(Decimal(expected))
        appended = _assert_conserved(before)
        assert len(appended) == 1, "even a zero adjustment writes exactly one accountable row"

    def test_a_third_decimal_is_rounded_to_the_columns_scale(self, app, db, world):
        """`Numeric(12,2)` silently rounds a 3-decimal input, so a 0.005
        adjustment vanishes. Pinned so the rounding is a decision, not a
        surprise.
        """
        _admin_user, headers = _admin(app)
        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"address_id": world["a1"].id, "adjustment": 0.005, "notes": "dust"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        _db.session.expire_all()
        landed = _place_balance(world["a1"].id)
        # The invariant that is backend-INDEPENDENT and is the actual subject:
        # the third decimal does not survive the column's scale.
        assert -landed.as_tuple().exponent <= 2, f"a third decimal survived: {landed}"
        assert landed != Decimal("7.005")
        # ...and the direction it rounds, pinned per backend: SQLite quantizes
        # half-EVEN (7.00, the 0.005 vanishes), Postgres numeric rounds half-UP
        # (7.01). Both are silent data loss on a money-adjacent figure.
        assert landed in (Decimal("7.00"), Decimal("7.01")), landed
        assert landed == Decimal("7.00"), (
            "on the SQLite backend the 0.005 adjustment vanishes entirely"
        )

    @pytest.mark.parametrize("spelling", ["snake", "camel"])
    def test_both_body_spellings_are_accepted_with_identical_effects(self, app, db, world, spelling):
        """`alias_generator=to_camel` with `populate_by_name=True` is what makes
        this work. Drop `populate_by_name` and the snake_case bodies the admin
        UI actually sends (BottleTracking.js names its fields `address_id` /
        `fine_amount`) all 400; drop the alias generator and every camelCase
        client breaks. Only one of the two runs in production, so the other
        rots silently.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        body = (
            {"address_id": world["a1"].id, "adjustment": 2, "notes": "n"}
            if spelling == "snake"
            else {"addressId": world["a1"].id, "adjustment": 2, "notes": "n"}
        )
        before = _place_balance(world["a1"].id)

        resp = fresh.post(f"{API}/bottles/adjustment", json=body, headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["address_id"] == world["a1"].id
        assert _place_balance(world["a1"].id) == before + Decimal("2")

    def test_camel_and_snake_initial_balance_and_fine_bodies_agree(self, app, db):
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        owner_snake, owner_camel = _user(), _user()
        snake_addr, camel_addr = _address(owner_snake), _address(owner_camel)

        snake = fresh.post(f"{API}/bottles/initial-balance",
                           json={"user_id": owner_snake.id, "address_id": snake_addr.id,
                                 "quantity": 4},
                           headers=headers)
        camel = fresh.post(f"{API}/bottles/initial-balance",
                           json={"userId": owner_camel.id, "addressId": camel_addr.id,
                                 "quantity": 4},
                           headers=headers)
        assert snake.status_code == camel.status_code == 200, (snake.get_json(), camel.get_json())
        assert _place_balance(snake_addr.id) == _place_balance(camel_addr.id) == Decimal("4.00")

        snake_fine = fresh.post(f"{API}/bottles/fines",
                                json={"address_id": snake_addr.id, "quantity": 1,
                                      "fine_amount": 9000},
                                headers=headers)
        camel_fine = fresh.post(f"{API}/bottles/fines",
                                json={"addressId": camel_addr.id, "quantity": 1,
                                      "fineAmount": 9000},
                                headers=headers)
        assert snake_fine.status_code == camel_fine.status_code == 200
        assert snake_fine.get_json()["data"]["fine_amount"] == camel_fine.get_json()["data"]["fine_amount"]

    def test_unknown_extra_fields_are_ignored_and_an_explicit_null_user_id_derives(
        self, app, db, world
    ):
        """If a `model_config` ever gains `extra='forbid'`, every slightly-stale
        admin UI build starts 400ing. And `"userId": null` must behave
        identically to omitting the key — `Optional[int]=None` plus
        `exclude_none=True` makes that true today.
        """
        _admin_user, headers = _admin(app)
        expected_owner = UserAddress.query.get(min(world["a1"].id, world["a2"].id)).user_id

        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"userId": None, "bogusField": 1, "adjustment": 2,
                  "address_id": world["a1"].id, "notes": "x"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["user_id"] == expected_owner

    def test_an_empty_body_names_the_missing_fields_by_their_CAMEL_alias(self, app, db, world):
        """`_validated_payload` returns a Response on failure and every route
        guards with `if not isinstance(data, dict): return data`. Forget that
        guard on a new route and the Response tuple is used as a dict —
        AttributeError -> 500.

        PINNED WRINKLE: `alias_generator=to_camel` makes pydantic report the
        error `loc` as the CAMEL alias, so the admin UI — whose form fields are
        named `address_id` / `fine_amount` — is told "addressId: Field required"
        about a field it does not have. Harmless today, but it is the reason a
        naive field-to-error mapping in the panel silently shows nothing.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        cases = [
            ("post", f"{API}/bottles/adjustment", {"addressId", "adjustment", "notes"}),
            ("post", f"{API}/bottles/initial-balance", {"addressId", "quantity"}),
            ("post", f"{API}/bottles/fines", {"addressId", "quantity", "fineAmount"}),
            ("put", f"{API}/bottles/fines/{world['fine'].id}", {"action"}),
        ]
        for method, url, required in cases:
            resp = _call(fresh, method, url, {}, headers)
            assert resp.status_code == 400, f"{url} -> {resp.status_code}"
            assert resp.get_json()["success"] is False
            prose = " ".join(resp.get_json()["errors"])
            assert "Field required" in prose, f"{url} -> {prose}"
            for field in required:
                assert field in prose, f"{url} did not name the missing field {field}: {prose}"

    def test_a_missing_address_id_is_a_404_and_writes_nothing(self, app, db, world):
        _admin_user, headers = _admin(app)
        before = _conservation_probe()
        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"address_id": 999999, "adjustment": 1, "notes": "x"},
            headers=headers,
        )
        assert resp.status_code == 404, resp.get_json()
        assert _assert_conserved(before) == []

    def test_an_out_of_scope_user_id_is_refused_by_the_adjustment_route(
        self, app, db, world
    ):
        """FIXED — the xfail is gone.

        WAS: `admin_adjust_balance` never called `_assert_user_in_scope` (only
        `issue_fine` did), so an explicit OUT-OF-SCOPE `user_id` was accepted
        and a stranger's name was booked onto the place's ledger. NOW all three
        admin write bodies go through the shared
        `BottleTrackingService._authorised_place_attribution` funnel, so the
        fence cannot be forgotten by one of them again.

        `user_id` stays OPTIONAL — the derived-attribution case is covered by
        `TestAdjustmentRoute`'s omitted-`user_id` tests and by
        `test_admin_place_write_without_member.py`. What is refused is an
        explicitly NAMED non-member.
        """
        _admin_user, headers = _admin(app)
        stranger = world["stranger"]          # owns `solo`, no address at the group
        before = _place_balance(world["a1"].id)

        resp = app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"user_id": stranger.id, "address_id": world["a1"].id,
                  "adjustment": 5, "notes": "x"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
        assert _place_balance(world["a1"].id) == before

    def test_the_refused_out_of_scope_write_leaves_NO_disclosure_on_the_stranger(
        self, app, db, world
    ):
        """Companion to the fence above, asserting the CONSEQUENCE rather than
        the status code, because the consequence is what the defect actually
        cost: the stranger's admin ledger view used to show a movement at a
        place they have no address at.

        UPDATED FROM THE PIN. This test previously asserted the DISCLOSURE — it
        required the leaked row to be present and skipped itself once the fence
        appeared. It now asserts the fence's real effect, and does so per-row
        (`no row`, not `some row missing`) so a partial leak cannot pass.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        stranger = world["stranger"]

        written = fresh.post(f"{API}/bottles/adjustment",
                             json={"user_id": stranger.id, "address_id": world["a1"].id,
                                   "adjustment": 5, "notes": "x"},
                             headers=headers)
        assert written.status_code == 400, written.get_data(as_text=True)
        _db.session.rollback()

        leaked = fresh.get(f"{API}/bottles/ledger?user_id={stranger.id}", headers=headers)
        rows = leaked.get_json()["data"]["items"]
        assert all(r["address_group_id"] != world["group"].id for r in rows), (
            "a row at a place the stranger has no address at is still disclosed"
        )

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_adjustment_is_refused_at_the_boundary(
        self, app, db, world, literal
    ):
        """FIXED — the xfail is gone.

        WAS: nothing checked `is_finite()`. Python's `json` module accepts the
        BARE `NaN` / `Infinity` / `-Infinity` literals, pydantic's plain `float`
        carried them, and `_as_decimal` did `Decimal(str(nan))` — so the value
        reached the DATABASE instead of the request boundary. On SQLite that was
        an unhandled IntegrityError -> 500; on Postgres `numeric` ACCEPTS 'NaN',
        so the place's stored balance was poisoned PERMANENTLY and
        `reconcile_balance` could not repair it (see `TestOnRealPostgres`).

        NOW there are two layers, and both are deliberate: `allow_inf_nan=False`
        on the three admin request models is the boundary, and
        `BottleTrackingService._as_decimal` carries the same refusal as the SSOT
        backstop for every non-HTTP caller — the same shape
        `CustomerLinkService._validated_bottles_leaving` already had for §7.1.

        All three literals are parametrised because they failed the old guards
        in three different ways; they must now all end at the same 400.
        """
        _admin_user, headers = _admin(app)
        body = json.dumps(
            {"address_id": world["a1"].id, "adjustment": 0.0, "notes": "x"}
        ).replace("0.0", literal)
        before = _place_balance(world["a1"].id)

        resp = app.test_client().post(f"{API}/bottles/adjustment", data=body,
                                      content_type="application/json", headers=headers)
        assert resp.status_code == 400, (
            f"{literal} was not refused at the boundary: {resp.status_code}"
        )
        _db.session.rollback()
        assert _place_balance(world["a1"].id) == before

    def test_a_NaN_adjustment_is_stopped_BEFORE_the_column_and_writes_nothing(
        self, app, db, world
    ):
        """UPDATED FROM THE PIN. This asserted the pre-fix numbers — a 500
        `INTERNAL_ERROR` from an unhandled IntegrityError, i.e. the value being
        stopped by the COLUMN rather than by any guard — and skipped itself once
        the guard appeared. It now asserts the fixed numbers.

        Kept alongside the parametrised boundary test above because the CLAIM is
        different: not merely "400", but that NaN never reaches the write path
        at all, so no ledger row is appended and the place's stored balance is
        byte-identical afterwards. Under the old behaviour that was true only
        by accident of SQLite's NOT NULL.
        """
        _admin_user, headers = _admin(app)
        body = json.dumps({"address_id": world["a1"].id, "adjustment": 0.0, "notes": "x"}
                          ).replace("0.0", "NaN")
        rows_before = BottleLedger.query.count()

        written = app.test_client().post(f"{API}/bottles/adjustment", data=body,
                                        content_type="application/json", headers=headers)
        assert written.status_code == 400, written.get_data(as_text=True)
        _db.session.rollback()
        assert BottleLedger.query.count() == rows_before
        assert _place_balance(world["a1"].id) == Decimal("7.00")

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity"])
    def test_an_INFINITE_adjustment_is_REFUSED_and_the_place_stays_finite(
        self, app, db, world, literal
    ):
        """UPDATED FROM THE PIN. This asserted the pre-fix numbers: a COMMITTED
        200, a non-finite stored balance, and a `reconcile_balance` that
        re-wrote the same poison because the ledger sum was non-finite too.

        The two signs stay parametrised SEPARATELY because they used to break
        different things and lumping them together would have overclaimed —
        `+Infinity` defeated `_validated_bottles_leaving`'s `max(0, place)` cap
        outright, while `-Infinity` left that particular clamp working
        (`max(0, -inf) == 0`) and instead made the place permanently
        un-departable. Both must now be refused identically, and the
        post-condition below is the strong one: the place is still FINITE, so
        every clamp downstream still works and reconcile is still meaningful.
        """
        _admin_user, headers = _admin(app)
        before = _place_balance(world["a1"].id)
        body = json.dumps({"address_id": world["a1"].id, "adjustment": 0.0, "notes": "x"}
                          ).replace("0.0", literal)

        written = app.test_client().post(f"{API}/bottles/adjustment", data=body,
                                        content_type="application/json", headers=headers)
        assert written.status_code == 400, written.get_data(as_text=True)

        _db.session.rollback()
        _db.session.expire_all()
        intact = _place_balance(world["a1"].id)
        assert intact.is_finite(), f"{literal} reached the stored balance"
        assert intact == before == Decimal("7.00"), intact
        # The clamp the poison used to defeat still behaves.
        assert max(Decimal("0.00"), intact) == Decimal("7.00")
        # ...and the repair button still repairs, because the ledger sum is finite.
        repaired = app.test_client().post(f"{API}/bottles/reconcile/{world['a1'].id}",
                                          headers=headers)
        assert repaired.status_code == 200, repaired.get_data(as_text=True)
        _db.session.expire_all()
        assert _place_balance(world["a1"].id) == Decimal("7.00")


# =========================================================================== #
# 10. POST /admin/bottles/initial-balance — a ONE-SHOT per place
# =========================================================================== #


class TestInitialBalanceRoute:
    def test_a_virgin_place_is_seeded_once_and_refuses_the_second_and_third_call(
        self, app, db
    ):
        """The guard is structural (`has_history OR balance != 0`), not
        key-based, and is checked under the FOR UPDATE lock. The "via the OTHER
        member" case is the one that regresses first if anyone re-keys the check
        to the address — two coworkers must not each seed the same office.
        """
        admin, headers = _admin(app)
        a1 = _address(_user(), title="office")
        a2 = _address(_user(), title="office")
        _group([a1, a2], admin=admin)
        low, high = sorted([a1, a2], key=lambda a: a.id)
        before = _conservation_probe()

        first = app.test_client().post(f"{API}/bottles/initial-balance",
                                       json={"address_id": high.id, "quantity": 4},
                                       headers=headers)
        assert first.status_code == 200, first.get_json()
        entry = first.get_json()["data"]
        assert entry["quantity"] == 4.0
        assert entry["balance_after"] == 4.0
        assert entry["idempotency_key"] is None, (
            "UPDATED: the scope-derived key is GONE. It was the defect, not the "
            "guard: `uq_bottle_ledger_idempotency` is UNIQUE on the KEY ALONE, so "
            "`_create_ledger_entry`'s duplicate lookup carries no scope predicate "
            "and a key left behind by a dissolved place — or one that survived a "
            "join re-stamp — swallowed a later legitimate seed for a DIFFERENT "
            "place behind a 200 echoing another customer's row. Adding a scope "
            "predicate to that lookup would turn the silent no-op into an "
            "IntegrityError 500. The structural guard below is the real one."
        )
        assert entry["event_type"] == BottleLedgerEventType.INITIAL_BALANCE.value
        assert _place_balance(high.id) == Decimal("4.00")

        for retry_address in (high, low):
            again = app.test_client().post(f"{API}/bottles/initial-balance",
                                           json={"address_id": retry_address.id, "quantity": 9},
                                           headers=headers)
            assert again.status_code == 400, again.get_json()
            assert again.get_json()["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"
            assert _place_balance(high.id) == Decimal("4.00")

        appended = _assert_conserved(before)
        assert len(appended) == 1

    def test_a_place_whose_ledger_NETS_TO_ZERO_still_refuses_a_new_opening_balance(
        self, app, db
    ):
        """`has_history or balance != 0` — if the OR ever collapses to just the
        balance check, a place with a full delivery history gets a brand new
        opening balance stacked on top of it, minting bottles with a
        legitimate-looking audit row.
        """
        admin, headers = _admin(app)
        owner = _user()
        addr = _address(owner)
        _deliver(owner, addr, 5, actor=admin)
        _return(owner, addr, 5, actor=admin)
        assert _place_balance(addr.id) == Decimal("0.00")
        before = _conservation_probe()

        resp = app.test_client().post(f"{API}/bottles/initial-balance",
                                      json={"address_id": addr.id, "quantity": 10},
                                      headers=headers)
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"
        assert _place_balance(addr.id) == Decimal("0.00")
        assert _assert_conserved(before) == []

    def test_a_zero_opening_balance_succeeds_and_BURNS_the_one_shot(self, app, db):
        """PINNED AS CURRENT BEHAVIOUR. A fat-fingered 0 writes a zero
        INITIAL_BALANCE row, which then blocks any future opening balance for
        that place (`has_history`) with no way back except an adjustment.
        """
        _admin_user, headers = _admin(app)
        addr = _address(_user())
        fresh = app.test_client()

        zero = fresh.post(f"{API}/bottles/initial-balance",
                          json={"address_id": addr.id, "quantity": 0}, headers=headers)
        assert zero.status_code == 200, zero.get_json()
        assert _place_balance(addr.id) == Decimal("0.00")

        retry = fresh.post(f"{API}/bottles/initial-balance",
                           json={"address_id": addr.id, "quantity": 12}, headers=headers)
        assert retry.status_code == 400
        assert retry.get_json()["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"

    def test_a_negative_opening_balance_succeeds_with_no_validation_at_all(self, app, db):
        """PINNED AS CURRENT BEHAVIOUR. "The customer starts owing us minus
        five bottles" is nonsense that then survives every clamp — which is
        precisely why `suggested_bottles_leaving`'s `max(0, place)` and the
        split's cap exist.
        """
        _admin_user, headers = _admin(app)
        addr = _address(_user())

        resp = app.test_client().post(f"{API}/bottles/initial-balance",
                                      json={"address_id": addr.id, "quantity": -5},
                                      headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert _place_balance(addr.id) == Decimal("-5.00")

    def test_an_omitted_notes_key_falls_back_to_the_service_default(self, app, db):
        """`_validated_payload` uses `model_dump(exclude_none=True)`, so an
        absent optional becomes an absent KEY, not a None value. Any caller
        doing `data['notes']` instead of `data.get('notes')` KeyErrors -> 400
        MISSING_KEY — and the adjustment route three lines away DOES use
        `data['notes']`.
        """
        _admin_user, headers = _admin(app)
        addr = _address(_user())

        resp = app.test_client().post(f"{API}/bottles/initial-balance",
                                      json={"address_id": addr.id, "quantity": 3},
                                      headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["notes"] == "Initial balance set by admin"

    def test_an_out_of_scope_user_id_is_refused_by_the_initial_balance_route(
        self, app, db, world
    ):
        """FIXED — the xfail is gone.

        WAS: `set_initial_balance` never called `_assert_user_in_scope`, so an
        explicit OUT-OF-SCOPE `user_id` was accepted — and worse than the
        adjustment case, because an initial balance is ONE-SHOT per place
        (`BOTTLE_INITIAL_BALANCE_EXISTS`): the place was PERMANENTLY stamped
        with a stranger and the seed could never be re-run.

        NOW it goes through the shared `_authorised_place_attribution` funnel
        with the other two admin write bodies. The last assertion is the
        load-bearing one: the refusal must also leave the ONE SHOT UNBURNT.
        """
        _admin_user, headers = _admin(app)
        virgin_a = _address(_user(), title="virgin")
        virgin_b = _address(_user(), title="virgin")
        admin = _user(role=UserRole.ADMIN, user_type=UserType.STAFF)
        _group([virgin_a, virgin_b], admin=admin)

        fresh = app.test_client()
        resp = fresh.post(
            f"{API}/bottles/initial-balance",
            json={"user_id": world["stranger"].id, "address_id": virgin_a.id, "quantity": 4},
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
        _db.session.rollback()
        assert BottleBalance.query.filter_by(address_id=virgin_a.id).first() is None
        assert BottleLedger.query.filter_by(address_id=virgin_a.id).count() == 0

        # THE ONE SHOT IS UNBURNT: the legitimate seed still succeeds afterwards
        # and is attributed to the place's own representative, not the stranger.
        seeded = fresh.post(f"{API}/bottles/initial-balance",
                            json={"address_id": virgin_a.id, "quantity": 4}, headers=headers)
        assert seeded.status_code == 200, seeded.get_json()
        representative = min(virgin_a.id, virgin_b.id)
        assert seeded.get_json()["data"]["user_id"] == (
            UserAddress.query.get(representative).user_id
        )
        assert _place_balance(virgin_a.id) == Decimal("4.00")


# =========================================================================== #
# 11. FINES — issue, list, waive, mark paid
# =========================================================================== #


class TestFineIssueRoute:
    def test_issuing_a_fine_derives_the_owner_freezes_the_scope_and_moves_no_bottles(
        self, app, db, world
    ):
        """The FINE_ISSUED row is quantity 0 BY DESIGN. "Fix" it to -quantity
        and the place is debited at issue AND again at payment.
        `place_balance_at_issue` is the only record of why the fine was
        justified once the balance moves on.
        """
        _admin_user, headers = _admin(app)
        representative = min(world["a1"].id, world["a2"].id)
        expected_owner = UserAddress.query.get(representative).user_id
        target = world["a2"] if world["a2"].id != representative else world["a1"]
        before = _conservation_probe()
        balance_before = _place_balance(target.id)

        resp = app.test_client().post(
            f"{API}/bottles/fines",
            json={"address_id": target.id, "quantity": 2, "fine_amount": 50000},
            headers=headers,
        )
        assert resp.status_code == 200, resp.get_json()
        fine = resp.get_json()["data"]
        assert fine["user_id"] == expected_owner
        assert fine["address_id"] == target.id
        assert fine["address_group_id"] == world["group"].id
        assert fine["status"] == BottleFineStatus.PENDING.value

        issued = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_ISSUED
        ).order_by(BottleLedger.id.desc()).first()
        assert issued.address_group_id == world["group"].id
        assert Decimal(str(issued.quantity)) == Decimal("0")
        assert issued.entry_metadata["fine_id"] == fine["id"]
        assert issued.entry_metadata["fine_quantity"] == 2.0
        assert issued.entry_metadata["fine_amount"] == 50000.0
        assert issued.entry_metadata["place_balance_at_issue"] == float(balance_before)

        assert _place_balance(target.id) == balance_before, "issuing a fine moves no bottles"
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("0")]

    def test_an_explicit_out_of_scope_user_id_is_refused_over_HTTP_with_its_code(
        self, app, db, world
    ):
        """The existing coverage for this fence asserts at the SERVICE level
        only. The HTTP surface additionally depends on `handle_api_exception`
        forwarding `ValidationError.error_code` into the envelope — a different
        code path that has never been asserted for this fence.
        """
        _admin_user, headers = _admin(app)
        before = _conservation_probe()
        fines_before = BottleFine.query.count()

        resp = app.test_client().post(
            f"{API}/bottles/fines",
            json={"user_id": world["stranger"].id, "address_id": world["a1"].id,
                  "quantity": 1, "fine_amount": 10000},
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["error_code"] == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
        assert BottleFine.query.count() == fines_before
        assert _assert_conserved(before) == []

    @pytest.mark.parametrize(
        "body,message",
        [
            ({"quantity": 0, "fine_amount": 10000}, "Fine quantity must be positive"),
            ({"quantity": -1, "fine_amount": 10000}, "Fine quantity must be positive"),
            ({"quantity": 1, "fine_amount": 0}, "Fine amount must be positive"),
            ({"quantity": 1, "fine_amount": -100}, "Fine amount must be positive"),
        ],
    )
    def test_zero_and_negative_quantity_or_amount_are_refused(self, app, db, world, body, message):
        """A fine of 0 bottles for 0 UZS is a real fat-finger, and
        `mark_fine_paid` on it would write a -0 ledger row that survives every
        non-zero filter.
        """
        _admin_user, headers = _admin(app)
        fines_before = BottleFine.query.count()
        before = _conservation_probe()

        resp = app.test_client().post(f"{API}/bottles/fines",
                                      json={"address_id": world["a1"].id, **body},
                                      headers=headers)
        assert resp.status_code == 400, resp.get_json()
        assert message in resp.get_json()["message"]
        assert BottleFine.query.count() == fines_before
        assert _assert_conserved(before) == []

    @pytest.mark.parametrize("field", ["quantity", "fine_amount"])
    def test_a_NaN_fine_is_a_400_not_an_unhandled_InvalidOperation(
        self, app, db, world, field
    ):
        """FIXED — the xfail is gone.

        WAS: nothing checked `is_finite()`, so the bare JSON `NaN` literal
        reached `_as_decimal` -> `Decimal('NaN')` and then `if qty <= 0`. That
        ORDERING comparison RAISES `decimal.InvalidOperation` — `decimal` is not
        IEEE-754, so it does NOT quietly return False — and nothing caught it:
        the route answered 500 INTERNAL_ERROR to a malformed request.

        NOW `_as_decimal` refuses the value before any comparison is attempted,
        so the positivity guards are never asked a question they cannot answer.
        """
        _admin_user, headers = _admin(app)
        payload = {"address_id": world["a1"].id, "quantity": 1, "fine_amount": 10000}
        payload[field] = 0.0
        body = json.dumps(payload).replace("0.0", "NaN")
        fines_before = BottleFine.query.count()

        resp = app.test_client().post(f"{API}/bottles/fines", data=body,
                                      content_type="application/json", headers=headers)
        assert resp.status_code == 400, f"a NaN {field} was accepted: {resp.get_data(as_text=True)}"
        assert BottleFine.query.count() == fines_before

    def test_a_NaN_fine_writes_no_row_and_moves_no_bottle(self, app, db, world):
        """UPDATED FROM THE PIN. This asserted the pre-fix numbers — a 500
        `INTERNAL_ERROR` per field — and skipped itself once the guard appeared.
        It now asserts a 400 per field.

        Kept, because it still carries the claim the sibling test above does not:
        BOTH fields, in one session, with no `bottle_fines` row and no movement
        of the place's balance. It was also the test that REFUTED the tempting
        stronger report ("the NaN is persisted"): it never was, because the
        raise happened before the INSERT. That distinction is why the INFINITY
        case below is a separate test — that one really did reach the database.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        fines_before = BottleFine.query.count()
        balance_before = _place_balance(world["a1"].id)

        for field in ("quantity", "fine_amount"):
            payload = {"address_id": world["a1"].id, "quantity": 1, "fine_amount": 10000}
            payload[field] = 0.0
            body = json.dumps(payload).replace("0.0", "NaN")
            resp = fresh.post(f"{API}/bottles/fines", data=body,
                              content_type="application/json", headers=headers)
            assert resp.status_code == 400, resp.get_data(as_text=True)
            _db.session.rollback()

        assert BottleFine.query.count() == fines_before, "a NaN fine must not be persisted"
        assert _place_balance(world["a1"].id) == balance_before

    @pytest.mark.parametrize("field", ["quantity", "fine_amount"])
    def test_an_INFINITE_fine_is_refused_before_the_positivity_guards(
        self, app, db, world, field
    ):
        """FIXED — the xfail is gone.

        WAS: `if qty <= 0` / `if amount <= 0` are both FALSE for
        `Decimal('Infinity')` — an ordering comparison against a decimal
        infinity is well-defined and simply false — so an INFINITE fine passed
        BOTH positivity guards and WAS persisted. This was the half of the
        non-finite defect that actually reached the database through the fines
        route, and note the asymmetry it exposes: `Decimal('-Infinity') <= 0` is
        True, so the negative sign was already caught by those same guards.

        NOW `_as_decimal` refuses every non-finite value before the guards run,
        so both signs end at the same 400.
        """
        _admin_user, headers = _admin(app)
        payload = {"address_id": world["a1"].id, "quantity": 1, "fine_amount": 10000}
        payload[field] = 0.0
        body = json.dumps(payload).replace("0.0", "Infinity")
        fines_before = BottleFine.query.count()

        resp = app.test_client().post(f"{API}/bottles/fines", data=body,
                                      content_type="application/json", headers=headers)
        assert resp.status_code == 400, (
            f"an infinite {field} was accepted: {resp.get_data(as_text=True)}"
        )
        assert BottleFine.query.count() == fines_before

    def test_no_INFINITE_fine_can_exist_to_poison_the_place_when_it_is_marked_paid(
        self, app, db, world
    ):
        """UPDATED FROM THE PIN. This asserted the pre-fix numbers: the infinite
        fine was ISSUED (200), then settled through the normal admin action, and
        `mark_fine_paid` wrote `-Decimal('Infinity')` through
        `_create_ledger_entry`, leaving the shared place's stored balance
        non-finite for good.

        It now asserts that the settlement path can never be reached, because
        the fine cannot be created — and it walks the SAME two steps rather than
        stopping at the 400, so the whole issue-then-settle chain stays covered:
        the fines list gains no PENDING row, and the place is untouched.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        fines_before = BottleFine.query.count()
        body = json.dumps(
            {"address_id": world["a1"].id, "quantity": 0.0, "fine_amount": 10000}
        ).replace("0.0", "Infinity")

        issued = fresh.post(f"{API}/bottles/fines", data=body,
                            content_type="application/json", headers=headers)
        assert issued.status_code == 400, issued.get_data(as_text=True)
        _db.session.rollback()
        assert BottleFine.query.count() == fines_before, (
            "an infinite fine was persisted and is waiting to be settled"
        )
        assert _place_balance(world["a1"].id) == Decimal("7.00")

        # There is no fine id to settle, so the poisoning step is unreachable by
        # construction — asserted rather than assumed, via the admin's own list.
        listed = fresh.get(f"{API}/bottles/fines", headers=headers).get_json()["data"]["items"]
        assert all(f["quantity"] not in (float("inf"), float("-inf")) for f in listed), listed
        assert _place_balance(world["a1"].id).is_finite()

    def test_address_id_zero_hits_the_dead_guard_instead_of_a_404(self, app, db, world):
        """PINNED. `create_bottle_fine` does
        `address_id = data.get('address_id'); if not address_id: ...`, which is
        unreachable for a MISSING field (pydantic requires it) — what it
        actually catches is `address_id: 0`, answering "address_id is required"
        where `resolve_scope` would have produced a 404. The equivalent guard is
        absent on the adjustment and initial-balance routes, so the three
        sibling routes disagree about what 0 means.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        fine = fresh.post(f"{API}/bottles/fines",
                          json={"address_id": 0, "quantity": 1, "fine_amount": 1000},
                          headers=headers)
        assert fine.status_code == 400
        assert "address_id is required" in " ".join(fine.get_json()["errors"])

        adjust = fresh.post(f"{API}/bottles/adjustment",
                            json={"address_id": 0, "adjustment": 1, "notes": "x"},
                            headers=headers)
        assert adjust.status_code == 404, "the sibling route treats 0 as a lookup miss"
        initial = fresh.post(f"{API}/bottles/initial-balance",
                             json={"address_id": 0, "quantity": 1}, headers=headers)
        assert initial.status_code == 404


class TestFineListRoute:
    def test_a_fine_row_labels_its_place_and_falls_back_to_the_address_title(self, app, db):
        """`serialize_bottle_fine_row` resolves the fine's FROZEN scope to a
        balance row and labels it; with no balance row it falls back to the
        fine's own `address_title`. A None `place_label` renders as a blank
        identity on a debt-collection screen.
        """
        admin, headers = _admin(app)
        owner_grouped = _user(first_name="Grouped", last_name="Person")
        grouped_a = _address(owner_grouped, title="office")
        grouped_b = _address(_user(), title="office")
        group = _group([grouped_a, grouped_b], admin=admin, label="Acme HQ")
        owner_solo = _user(first_name="Solo", last_name="Person")
        solo = _address(owner_solo, title="corner shop")

        _deliver(owner_grouped, grouped_a, 6, actor=admin)
        svc = BottleTrackingService()
        svc.issue_fine(user_id=None, address_id=grouped_a.id, quantity=Decimal("1"),
                       fine_amount=Decimal("1000"), actor_user_id=admin.id)
        # The solo address has NO balance row: a fine is the first thing to
        # touch it, and FINE_ISSUED is quantity 0 so no row is created... except
        # `get_or_create_balance` runs anyway. Either way `place_label` must be
        # non-null, which is what this asserts.
        svc.issue_fine(user_id=owner_solo.id, address_id=solo.id, quantity=Decimal("1"),
                       fine_amount=Decimal("2000"), actor_user_id=admin.id)
        _db.session.commit()

        rows = app.test_client().get(f"{API}/bottles/fines", headers=headers).get_json()["data"]["items"]
        by_address = {r["address_id"]: r for r in rows}
        assert by_address[grouped_a.id]["place_label"] == "Acme HQ"
        assert by_address[grouped_a.id]["address_group_id"] == group.id
        assert by_address[solo.id]["place_label"] == "corner shop"
        assert by_address[solo.id]["address_group_id"] is None
        # Exact identities, not truthiness: `user_name` is the only thing naming
        # WHO the debt-collection screen is about, and the grouped fine's
        # attribution was DERIVED from the place's representative member.
        assert by_address[grouped_a.id]["user_name"] == "Grouped Person"
        assert by_address[solo.id]["user_name"] == "Solo Person"
        assert by_address[grouped_a.id]["address_title"] == "office"
        assert by_address[solo.id]["address_title"] == "corner shop"
        for row in rows:
            assert row["place_label"], f"blank place_label on {row}"

    def test_fines_filter_by_status_by_user_and_by_both(self, app, db):
        admin, headers = _admin(app)
        alice, bob = _user(first_name="Ali"), _user(first_name="Bob")
        addr_a, addr_b = _address(alice), _address(bob)
        svc = BottleTrackingService()
        made = {}
        for owner, addr, status, amount in [
            (alice, addr_a, BottleFineStatus.PENDING, 1000),
            (alice, addr_a, BottleFineStatus.WAIVED, 2000),
            (alice, addr_a, BottleFineStatus.PAID, 3000),
            (bob, addr_b, BottleFineStatus.PENDING, 4000),
            (bob, addr_b, BottleFineStatus.WAIVED, 5000),
            (bob, addr_b, BottleFineStatus.INVOICED, 6000),
        ]:
            fine = svc.issue_fine(user_id=owner.id, address_id=addr.id, quantity=Decimal("1"),
                                  fine_amount=Decimal(amount), actor_user_id=admin.id)
            fine.status = status
            _db.session.commit()
            made[fine.id] = (owner.id, status)

        fresh = app.test_client()

        def ids(query):
            resp = fresh.get(f"{API}/bottles/fines{query}", headers=headers)
            assert resp.status_code == 200, resp.get_json()
            return {r["id"] for r in resp.get_json()["data"]["items"]}, resp.get_json()["data"]

        pending, _ = ids("?status=pending")
        assert pending == {f for f, (_, s) in made.items() if s is BottleFineStatus.PENDING}
        waived, _ = ids("?status=waived")
        assert waived == {f for f, (_, s) in made.items() if s is BottleFineStatus.WAIVED}
        alice_all, _ = ids(f"?user_id={alice.id}")
        assert alice_all == {f for f, (u, _) in made.items() if u == alice.id}
        combined, _ = ids(f"?status=pending&user_id={alice.id}")
        assert combined == pending & alice_all

        _page, data = ids("?page=2&per_page=2")
        assert data["total"] == 6 and data["pages"] == 3 and data["page"] == 2

    def test_fines_are_ordered_issued_at_DESC(self, app, db):
        admin, headers = _admin(app)
        owner = _user()
        addr = _address(owner)
        svc = BottleTrackingService()
        for i in range(3):
            fine = svc.issue_fine(user_id=owner.id, address_id=addr.id, quantity=Decimal("1"),
                                  fine_amount=Decimal("1000"), actor_user_id=admin.id)
            fine.issued_at = datetime(2026, 1, i + 1, tzinfo=UTC)
            _db.session.commit()

        rows = app.test_client().get(f"{API}/bottles/fines", headers=headers).get_json()["data"]["items"]
        stamps = [r["issued_at"] for r in rows]
        assert stamps == sorted(stamps, reverse=True)


class TestFineUpdateRoute:
    @pytest.mark.parametrize("action", ["delete", "WAIVE", "mark-paid", "", "waive "])
    def test_the_action_enum_is_the_only_thing_between_a_typo_and_a_debit(
        self, app, db, world, action
    ):
        """The route branches `if action == 'waive' else mark_paid`, so the
        pydantic pattern `^(waive|mark_paid)$` is the ONLY thing stopping an
        unrecognised string from silently taking the mark_paid branch and
        destroying bottles.
        """
        _admin_user, headers = _admin(app)
        before = _place_balance(world["a1"].id)
        status_before = world["fine"].status

        resp = app.test_client().put(f"{API}/bottles/fines/{world['fine'].id}",
                                     json={"action": action}, headers=headers)
        assert resp.status_code == 400, f"{action!r} -> {resp.status_code} {resp.get_json()}"
        _db.session.expire_all()
        assert BottleFine.query.get(world["fine"].id).status == status_before
        assert _place_balance(world["a1"].id) == before

    def test_waiving_twice_is_a_409_and_writes_no_second_reversal(self, app, db, world):
        """FINE_REVERSED carries no idempotency key, so the status guard is the
        ONLY thing stopping duplicate reversal rows. Loosen it to allow
        re-waiving "with new notes" and the ledger grows a row per click.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        fine_id = world["fine"].id
        balance_before = _place_balance(world["a1"].id)
        before = _conservation_probe()

        first = fresh.put(f"{API}/bottles/fines/{fine_id}",
                          json={"action": "waive", "notes": "goodwill"}, headers=headers)
        assert first.status_code == 200, first.get_json()
        assert first.get_json()["data"]["status"] == BottleFineStatus.WAIVED.value
        assert first.get_json()["data"]["waived_at"]
        assert first.get_json()["data"]["waived_by"]
        reversals = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_REVERSED).count()
        assert reversals == 1
        assert _place_balance(world["a1"].id) == balance_before, "a waiver moves no bottles"

        for action in ("waive", "mark_paid"):
            again = fresh.put(f"{API}/bottles/fines/{fine_id}",
                              json={"action": action}, headers=headers)
            assert again.status_code == 409, f"{action} -> {again.get_json()}"
            assert "already waived" in again.get_json()["message"]

        assert BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_REVERSED).count() == 1
        assert _place_balance(world["a1"].id) == balance_before
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("0")]

    def test_marking_a_fine_paid_debits_the_place_exactly_once(self, app, db, world):
        """Two independent protections overlap here: the status guard and the
        `fine_paid:<id>` idempotency key. Remove either and the other still
        passes the happy path — but a status-guard bypass PLUS a key change
        double-debits. Asserting the before/after pair is what stops a second
        debit hiding.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        fine_id = world["fine"].id
        assert _place_balance(world["a1"].id) == Decimal("7.00")
        before = _conservation_probe()

        paid = fresh.put(f"{API}/bottles/fines/{fine_id}",
                         json={"action": "mark_paid"}, headers=headers)
        assert paid.status_code == 200, paid.get_json()
        assert paid.get_json()["data"]["status"] == BottleFineStatus.PAID.value
        assert paid.get_json()["data"]["paid_at"]

        row = BottleLedger.query.filter_by(idempotency_key=f"fine_paid:{fine_id}").one()
        assert Decimal(str(row.quantity)) == Decimal("-2")
        assert Decimal(str(row.balance_after)) == Decimal("5.00")
        assert row.address_group_id == world["group"].id
        assert _place_balance(world["a1"].id) == Decimal("5.00")

        again = fresh.put(f"{API}/bottles/fines/{fine_id}",
                          json={"action": "mark_paid"}, headers=headers)
        assert again.status_code == 409, again.get_json()
        assert BottleLedger.query.filter_by(idempotency_key=f"fine_paid:{fine_id}").count() == 1
        assert _place_balance(world["a1"].id) == Decimal("5.00")

        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("-2")]

    def test_a_missing_fine_id_is_a_404(self, app, db):
        _admin_user, headers = _admin(app)
        resp = app.test_client().put(f"{API}/bottles/fines/999999",
                                     json={"action": "waive"}, headers=headers)
        assert resp.status_code == 404, resp.get_json()


class TestFineFrozenScopeOutlivesItsBalanceRow:
    """A fine's `address_group_id` is FROZEN at issue so the FINE_ISSUED /
    FINE_PAID pair cannot be split across two ledgers. But `_create_ledger_entry`
    calls `get_or_create_balance` — a CREATE, not a lookup — so a frozen scope
    whose balance row has since been deleted is RECREATED, and nothing resolves
    to it. Both reachable states are demonstrated below.
    """

    def test_paying_a_fine_issued_before_its_address_joined_a_place(self, app, db):
        """FIXED — the xfail is gone.

        WAS: a fine issued while its address was UNGROUPED froze
        `address_group_id=NULL`. After the address JOINED a place,
        `absorb_address_into_group` had deleted its own-scope balance row, so
        FINE_PAID's `get_or_create_balance` CREATED a new address-scoped row at
        -quantity for a GROUPED address. `resolve_scope` goes to the group, so
        nothing ever resolved to that row: the customer's real place balance
        never moved and the bottles were destroyed into an unreachable scope.

        NOW the join CARRIES the frozen reference — `absorb_address_into_group`
        re-stamps `bottle_fines` with the same selector it uses for
        `bottle_ledger` — so the settlement lands on the place. `assert_reachable`
        is the second layer behind it.
        """
        admin, headers = _admin(app)
        owner = _user()
        joining = _address(owner, title="shop")
        _deliver(owner, joining, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=owner.id, address_id=joining.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        assert fine.address_group_id is None
        fine_id = fine.id

        other = _address(_user(), title="shop")
        group = _group([joining, other], admin=admin)
        assert _place_balance(joining.id) == Decimal("6.00")
        assert BottleBalance.query.filter_by(address_id=joining.id).first() is None

        resp = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                     json={"action": "mark_paid"}, headers=headers)
        assert resp.status_code == 200, resp.get_json()
        _db.session.expire_all()

        assert _place_balance(joining.id) == Decimal("4.00"), (
            "the 2 bottles must come off the PLACE the customer actually has"
        )
        assert BottleBalance.query.filter_by(address_id=joining.id).first() is None, (
            "no unreachable address-scoped row may be created for a grouped address"
        )
        assert group.id is not None

    def test_the_unreachable_row_a_fine_payment_creates_is_visible_on_the_balances_screen(
        self, app, db
    ):
        """Companion to the xfail above, asserting BOTH SIDES of the split.

        This defect CONSERVES BOTTLES GLOBALLY — the coupled -2 moves Σ balances
        by exactly -2 — while corrupting scope attribution completely, so
        `_assert_conserved` PASSES here. That is asserted deliberately: it is the
        proof that the global oracle alone is blind to this class of bug, and the
        reason every assertion below is PER SCOPE.
        """
        admin, headers = _admin(app)
        owner = _user()
        joining = _address(owner, title="shop")
        _deliver(owner, joining, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=owner.id, address_id=joining.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        fine_id = fine.id
        group = _group([joining, _address(_user(), title="shop")], admin=admin)
        assert _place_balance(joining.id) == Decimal("6.00")
        before = _conservation_probe()

        paid = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                     json={"action": "mark_paid"}, headers=headers)
        assert paid.status_code == 200, paid.get_json()
        _db.session.expire_all()
        if _place_balance(joining.id) == Decimal("4.00"):
            pytest.skip("the frozen-scope defect is fixed — see the xfail above")

        # (1) GLOBAL conservation holds — the oracle sees nothing wrong.
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("-2")]

        # (2) PER SCOPE it is destruction: the real place never moved...
        assert _place_balance(joining.id) == Decimal("6.00")
        assert _ledger_sum(BottleScope.for_group(group.id)) == Decimal("6.00")
        # ...and the 2 bottles sit in an address scope nothing resolves to.
        assert _ledger_sum(BottleScope.for_address(joining.id)) == Decimal("-2")

        # (3) The debit is invisible on the place ledger the admin actually opens.
        place_ledger = app.test_client().get(f"{API}/bottles/ledger/{joining.id}",
                                             headers=headers).get_json()["data"]["items"]
        assert appended[0].id not in {i["id"] for i in place_ledger}, (
            "the FINE_PAID row must be missing from the place drawer — that is the "
            "defect: a debit no admin screen can see"
        )

        # (4) ...while the balances screen shows it as a phantom place.
        rows = app.test_client().get(f"{API}/bottles/balances", headers=headers
                                     ).get_json()["data"]["items"]
        orphan = [r for r in rows if r["address_id"] == joining.id]
        assert orphan and orphan[0]["balance"] == -2.0, (
            "the defect's shape has changed; re-verify the report"
        )
        assert orphan[0]["is_shared_place"] is False
        real = [r for r in rows if r["address_group_id"] == group.id]
        assert len(real) == 1 and real[0]["balance"] == 6.0, (
            "one physical place is now TWO rows on the admin's screen"
        )

    def test_waiving_a_fine_issued_before_the_join_creates_NO_orphan_row(
        self, app, db
    ):
        """FIXED — the xfail is gone.

        WAS the third face of the same defect, and the one that showed it is
        `get_or_create_balance` being a CREATE that is wrong, not the quantity:
        WAIVING a fine writes a quantity-ZERO FINE_REVERSED row, moves no bottles
        at all — and STILL materialised a `bottle_balances` row in the frozen
        (now unreachable) scope. The balances screen grew a phantom 0.00 place
        for an address that already belonged to a shared place, and the list's
        total/pages moved under the admin.
        """
        admin, headers = _admin(app)
        owner = _user()
        joining = _address(owner, title="shop")
        _deliver(owner, joining, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=owner.id, address_id=joining.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        fine_id = fine.id
        _group([joining, _address(_user(), title="shop")], admin=admin)
        assert BottleBalance.query.filter_by(address_id=joining.id).first() is None
        rows_before = BottleBalance.query.count()

        waived = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                       json={"action": "waive"}, headers=headers)
        assert waived.status_code == 200, waived.get_json()
        _db.session.expire_all()

        assert _place_balance(joining.id) == Decimal("6.00"), "a waiver moves no bottles"
        assert BottleBalance.query.count() == rows_before, (
            "a zero-quantity waiver must not materialise a balance row at all"
        )
        assert BottleBalance.query.filter_by(address_id=joining.id).first() is None

    def test_the_zero_row_a_waiver_creates_is_a_phantom_place_on_the_balances_screen(
        self, app, db
    ):
        """Companion to the xfail above, asserting the CONSEQUENCE. Global
        conservation is untouched (the waiver's quantity is 0), so no
        conservation oracle anywhere can see this — only a per-scope row count
        can.
        """
        admin, headers = _admin(app)
        owner = _user()
        joining = _address(owner, title="shop")
        _deliver(owner, joining, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=owner.id, address_id=joining.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        fine_id = fine.id
        group = _group([joining, _address(_user(), title="shop")], admin=admin)
        fresh = app.test_client()
        total_before = fresh.get(f"{API}/bottles/balances", headers=headers
                                 ).get_json()["data"]["total"]
        before = _conservation_probe()

        assert fresh.put(f"{API}/bottles/fines/{fine_id}",
                         json={"action": "waive"}, headers=headers).status_code == 200
        _db.session.expire_all()
        if BottleBalance.query.filter_by(address_id=joining.id).first() is None:
            pytest.skip("the frozen-scope defect is fixed — see the xfail above")

        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("0")], (
            "the waiver is a zero move — conservation is blind to this by construction"
        )
        data = fresh.get(f"{API}/bottles/balances", headers=headers).get_json()["data"]
        assert data["total"] == total_before + 1
        phantom = next(r for r in data["items"] if r["address_id"] == joining.id)
        assert phantom["balance"] == 0.0
        assert phantom["is_shared_place"] is False
        assert phantom["address_group_id"] is None, (
            "an address that IS in a place now has a second, place-less row"
        )
        real = next(r for r in data["items"] if r["address_group_id"] == group.id)
        assert real["balance"] == 6.0
        assert joining.id in real["member_address_ids"], (
            "the same address is on the screen twice: once as its place, once as itself"
        )

    def test_paying_a_fine_issued_before_its_place_dissolved(self, app, db):
        """FIXED — the xfail is gone.

        WAS the mirror of the join case. A fine issued at a group that later
        DISSOLVES kept a resolvable frozen group scope (the `AddressGroup` row is
        deliberately KEPT), so FINE_PAID recreated an ORPHANED group-scoped
        `bottle_balances` row at -quantity for a MEMBERLESS group —
        resurrecting the `orphaned_place_balances` violation §7.3 exists to
        close, and producing a balances row whose `representative_address_id` is
        None, so every admin-UI row action on it posted `undefined`.

        NOW the dissolve carries the SURVIVOR's frozen fine scopes out of the
        group alongside its ledger rows, and `assert_reachable` refuses to mint a
        group-scoped balance row for a group with no members.
        """
        admin, headers = _admin(app)
        owner_a, owner_b = _user(), _user()
        a1, a2 = _address(owner_a, title="office"), _address(owner_b, title="office")
        group = _group([a1, a2], admin=admin)
        _deliver(owner_a, a1, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        assert fine.address_group_id == group.id
        fine_id = fine.id

        removed = app.test_client().delete(
            f"{API}/place-groups/{group.id}/addresses/{a2.id}",
            json={"reason": "moved out"}, headers=headers,
        )
        assert removed.status_code == 200, removed.get_json()
        assert removed.get_json()["data"]["dissolved"] is True
        assert BottleBalance.query.filter_by(address_group_id=group.id).first() is None
        assert _place_balance(a1.id) == Decimal("6.00")

        paid = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                     json={"action": "mark_paid"}, headers=headers)
        assert paid.status_code == 200, paid.get_json()
        _db.session.expire_all()

        assert _place_balance(a1.id) == Decimal("4.00"), (
            "the debit must land on the surviving address's real place"
        )
        assert BottleBalance.query.filter_by(address_group_id=group.id).first() is None, (
            "no orphaned group-scoped balance row may be recreated for a memberless group"
        )

    def test_the_orphaned_group_row_a_fine_payment_recreates_has_no_action_target(
        self, app, db
    ):
        """Companion to the xfail above. `_scope_member_address_ids` returns []
        for a memberless group, so `representative_address_id` is None and
        `placeAddressIdOf` in BottleTracking.js yields `undefined` — every row
        action posts `/admin/bottles/ledger/undefined`.
        """
        admin, headers = _admin(app)
        owner_a, owner_b = _user(), _user()
        a1, a2 = _address(owner_a, title="office"), _address(owner_b, title="office")
        group = _group([a1, a2], admin=admin)
        _deliver(owner_a, a1, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        fine_id = fine.id
        dissolved = app.test_client().delete(
            f"{API}/place-groups/{group.id}/addresses/{a2.id}",
            json={"reason": "moved out"}, headers=headers,
        )
        assert dissolved.status_code == 200, dissolved.get_json()
        assert _place_balance(a1.id) == Decimal("6.00")
        before = _conservation_probe()

        paid = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                     json={"action": "mark_paid"}, headers=headers)
        assert paid.status_code == 200, paid.get_json()
        _db.session.expire_all()
        if BottleBalance.query.filter_by(address_group_id=group.id).first() is None:
            pytest.skip("the dissolved-scope defect is fixed — see the xfail above")

        # Globally conserved, per-scope destroyed — the same blind spot as the
        # join case, asserted from both ends.
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("-2")]
        assert _place_balance(a1.id) == Decimal("6.00"), (
            "the surviving member's real place never moved"
        )
        assert Decimal(str(
            BottleBalance.query.filter_by(address_group_id=group.id).one().balance
        )) == Decimal("-2.00")

        rows = app.test_client().get(f"{API}/bottles/balances", headers=headers
                                     ).get_json()["data"]["items"]
        orphan = next(r for r in rows if r["address_group_id"] == group.id)
        assert orphan["balance"] == -2.0
        assert orphan["representative_address_id"] is None
        assert orphan["member_address_ids"] == []
        assert orphan["member_names"] == []
        assert orphan["place_label"] == f"Place #{group.id}"


# =========================================================================== #
# 12. POST /admin/bottles/reconcile/<address_id>
# =========================================================================== #


class TestReconcileRoute:
    def test_reconciling_a_clean_place_is_a_no_op_and_reports_it_identically_at_both_members(
        self, app, db, world
    ):
        """The UI reads `res.data.discrepancy` (it used to read a `difference`
        key that never existed, so the warning branch had never fired). Rename
        that key again and the admin is told "Balance is consistent" after a
        destructive correction.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        rows_before = BottleLedger.query.count()

        via_a = fresh.post(f"{API}/bottles/reconcile/{world['a1'].id}", headers=headers)
        via_b = fresh.post(f"{API}/bottles/reconcile/{world['a2'].id}", headers=headers)
        assert via_a.status_code == via_b.status_code == 200
        assert via_a.get_json()["data"] == via_b.get_json()["data"], (
            "both member addresses reconcile the PLACE, not the address"
        )
        assert via_a.get_json()["data"] == {
            "address_group_id": world["group"].id,
            "address_id": None,
            "previous_balance": 7.0,
            "recalculated_balance": 7.0,
            "discrepancy": 0.0,
            "corrected": False,
        }
        assert _place_balance(world["a1"].id) == Decimal("7.00")
        assert BottleLedger.query.count() == rows_before

    def test_reconciling_a_DRIFTED_place_destroys_the_stored_figure_with_no_audit_row(
        self, app, db
    ):
        """PINNED AS CURRENT BEHAVIOUR. This is the one route on the axis that
        can silently destroy an admin-entered number with NO ledger row and only
        a `logger.warning`, and it is reachable by any MANAGER. The merge
        review's whole design (backfill first, then coupled corrections) exists
        to make this a no-op; anything that reintroduces drift makes this button
        dangerous again.
        """
        _admin_user, headers = _admin(app)
        owner = _user()
        drifted = _address(owner, title="depot")
        _manufacture_drift(drifted, 20)
        assert _place_balance(drifted.id) == Decimal("20.00")
        rows_before = BottleLedger.query.count()

        resp = app.test_client().post(f"{API}/bottles/reconcile/{drifted.id}", headers=headers)

        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["corrected"] is True
        assert data["discrepancy"] == 20.0
        assert data["previous_balance"] == 20.0 and data["recalculated_balance"] == 0.0
        assert data["address_id"] == drifted.id and data["address_group_id"] is None
        _db.session.expire_all()
        assert _place_balance(drifted.id) == Decimal("0.00")
        assert BottleLedger.query.count() == rows_before, (
            "no ledger entry explains the destruction — only a logger.warning does"
        )

    def test_reconcile_404s_on_a_missing_address(self, app, db):
        _admin_user, headers = _admin(app)
        resp = app.test_client().post(f"{API}/bottles/reconcile/999999", headers=headers)
        assert resp.status_code == 404, resp.get_json()

    def test_reconciling_a_place_that_never_moved_a_bottle_creates_NO_row(self, app, db):
        """FIXED — the xfail is gone.

        WAS: `reconcile_balance` called `get_or_create_balance`, so a read-shaped
        "check this is consistent" action MUTATED. POSTing it against a place
        that has never moved a bottle created a 0.00 `bottle_balances` row while
        reporting `discrepancy 0.0 / corrected false`. Clicking Reconcile across
        the balances screen slowly filled the table with zero rows, changing the
        list's total/pages and the dashboard's `places_with_balance` denominator.

        NOW it takes the row with `get_balance_row` — lock, never create — and an
        absent row is reported as zeros with nothing written. Minting a row here
        is also how the `orphaned_place_balances` class comes back.
        """
        _admin_user, headers = _admin(app)
        virgin = _address(_user(), title="never used")
        fresh = app.test_client()
        rows_before = BottleBalance.query.count()
        total_before = fresh.get(f"{API}/bottles/balances", headers=headers
                                 ).get_json()["data"]["total"]

        resp = fresh.post(f"{API}/bottles/reconcile/{virgin.id}", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["data"]["corrected"] is False

        _db.session.expire_all()
        assert BottleBalance.query.count() == rows_before, (
            "a consistency CHECK must not create a balance row"
        )
        assert fresh.get(f"{API}/bottles/balances", headers=headers
                         ).get_json()["data"]["total"] == total_before

    def test_the_zero_row_reconcile_creates_reaches_the_dashboard_denominator(self, app, db):
        """Companion to the xfail above, asserting the CONSEQUENCE. The row is
        at 0.00 so `places_with_balance` (which filters `> 0`) is untouched, but
        the balances LIST total is not — which is the number an admin paginates
        against.
        """
        _admin_user, headers = _admin(app)
        virgin = _address(_user(), title="never used")
        fresh = app.test_client()
        before = fresh.get(f"{API}/bottles/balances", headers=headers).get_json()["data"]["total"]

        fresh.post(f"{API}/bottles/reconcile/{virgin.id}", headers=headers)
        _db.session.expire_all()
        after = fresh.get(f"{API}/bottles/balances", headers=headers).get_json()["data"]["total"]

        if after == before:
            pytest.skip("reconcile no longer creates a row — see the xfail above")
        assert after == before + 1
        row = BottleBalance.query.filter_by(address_id=virgin.id).one()
        assert Decimal(str(row.balance)) == Decimal("0.00")
        dashboard = fresh.get(f"{API}/bottles/dashboard", headers=headers).get_json()["data"]
        assert dashboard["places_with_balance"] == BottleBalance.query.filter(
            BottleBalance.balance > 0).count()

    def test_reconcile_is_a_no_op_after_a_reviewed_merge_converged_the_two_figures(
        self, app, db
    ):
        """The convergence guarantee, observed from THIS route: after a reviewed
        merge `get_place_balance == ledger_sum`, so the destructive button has
        nothing to destroy. Reconcile is the honest oracle for that claim
        because it is the only production reader that compares both figures.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        x_owner, y_owner = _user(), _user()
        x = _address(x_owner, title="x")
        y = _address(y_owner, title="y")
        _deliver(x_owner, x, 6, actor=admin)
        _manufacture_drift(y, 20)

        preview = fresh.get(f"{API}/place-groups/merge-preview?address_ids={x.id},{y.id}",
                            headers=headers)
        assert preview.status_code == 200, preview.get_json()
        entry_ids = preview.get_json()["data"]["entry_ids"]

        created = fresh.post(f"{API}/place-groups",
                             json={"addressIds": [x.id, y.id], "reason": "same office",
                                   "previewEntryIds": entry_ids, "resultingBalance": 10},
                             headers=headers)
        assert created.status_code == 201, created.get_json()
        group_id = created.get_json()["data"]["place_group_id"]

        _db.session.expire_all()
        assert _place_balance(x.id) == Decimal("10.00")
        assert _ledger_sum(BottleScope.for_group(group_id)) == Decimal("10.00")

        reconciled = fresh.post(f"{API}/bottles/reconcile/{x.id}", headers=headers)
        assert reconciled.status_code == 200, reconciled.get_json()
        assert reconciled.get_json()["data"]["discrepancy"] == 0.0
        assert reconciled.get_json()["data"]["corrected"] is False


# =========================================================================== #
# 13. PLACE-GROUP ROUTE FENCES AND CODES
# =========================================================================== #


class TestPlaceGroupCreationFences:
    def test_fewer_than_two_addresses_and_a_duplicate_pair_fail_at_two_DIFFERENT_layers(
        self, app, db
    ):
        """The route checks `len(address_ids)` on the RAW list while the service
        checks the SET — so `[a1, a1]` passes the route and is caught only by
        the service's own `PLACE_GROUP_MIN_ADDRESSES`. Remove the service guard
        and a "group" of one address with itself is created, breaking every
        downstream assumption that a place has members.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        a1 = _address(_user())
        a2 = _address(_user())
        groups_before = AddressGroup.query.count()

        empty = fresh.post(f"{API}/place-groups", json={"addressIds": [], "reason": "r"},
                           headers=headers)
        assert empty.status_code == 400
        assert "At least 2 addressIds are required" in " ".join(empty.get_json()["errors"])

        single = fresh.post(f"{API}/place-groups", json={"addressIds": [a1.id], "reason": "r"},
                            headers=headers)
        assert single.status_code == 400
        assert "At least 2 addressIds are required" in " ".join(single.get_json()["errors"])

        duplicate = fresh.post(f"{API}/place-groups",
                               json={"addressIds": [a1.id, a1.id], "reason": "r"},
                               headers=headers)
        assert duplicate.status_code == 400, duplicate.get_json()
        assert duplicate.get_json()["data"]["error_code"] == "PLACE_GROUP_MIN_ADDRESSES"

        blank_reason = fresh.post(f"{API}/place-groups",
                                  json={"addressIds": [a1.id, a2.id], "reason": "   "},
                                  headers=headers)
        assert blank_reason.status_code == 400
        assert "reason is required" in " ".join(blank_reason.get_json()["errors"])

        assert AddressGroup.query.count() == groups_before
        _db.session.expire_all()
        assert UserAddress.query.get(a1.id).address_group_id is None

    @pytest.mark.parametrize(
        "kind,code",
        [
            ("grocery", "PLACE_GROUP_GROCERY_MEMBER"),
            ("entity", "PLACE_GROUP_ENTITY_MEMBER"),
            ("staff", "PLACE_GROUP_ENTITY_MEMBER"),
        ],
    )
    def test_grocery_entity_and_staff_owners_are_fenced_out(self, app, db, kind, code):
        """The grocery fence protects the corporate-contract COD mirror —
        grouping a grocery account's address into a shared pool would let a
        place's bottle/COD state cross into that mirror. The role check is
        `o_role != CUSTOMER`, which also excludes staff; an OR/AND slip in that
        compound condition opens one of the two.
        """
        _admin_user, headers = _admin(app)
        normal = _address(_user())
        if kind == "grocery":
            bad_owner = _user(user_type=UserType.ENTITY,
                              entity_subtype=EntitySubtype.GROCERY_STORE)
            assert bad_owner.is_grocery_store is True
        elif kind == "entity":
            bad_owner = _user(user_type=UserType.ENTITY, entity_subtype=EntitySubtype.WORKPLACE)
        else:
            bad_owner = _user(role=UserRole.OPERATOR, user_type=UserType.STAFF)
        bad = _address(bad_owner)
        groups_before = AddressGroup.query.count()

        resp = app.test_client().post(f"{API}/place-groups",
                                      json={"addressIds": [normal.id, bad.id], "reason": "r"},
                                      headers=headers)
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["data"]["error_code"] == code
        assert AddressGroup.query.count() == groups_before
        _db.session.expire_all()
        assert UserAddress.query.get(normal.id).address_group_id is None

    def test_an_already_grouped_address_cannot_be_grouped_again(self, app, db):
        """This membership fence is HALF the deadlock argument for the join's
        late-create branch: "a removal requires the address GROUPED, a join
        requires it UNGROUPED", which is what guarantees only one of two
        concurrent transactions takes a lock on the address row. Weakening it to
        a warning invalidates the lock-ordering PROOF, not just this response
        code.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        owner_a1, owner_a2, owner_a3, owner_a4 = (_user() for _ in range(4))
        a1, a2 = _address(owner_a1), _address(owner_a2)
        a3, a4 = _address(owner_a3), _address(owner_a4)
        g1 = _group([a1, a2], admin=admin)
        g2 = _group([a3, a4], admin=admin)
        _deliver(owner_a1, a1, 6, actor=admin)
        _deliver(owner_a3, a3, 4, actor=admin)
        before = _conservation_probe()
        members = {
            g1.id: CustomerLinkService().get_place_group_address_ids(g1.id),
            g2.id: CustomerLinkService().get_place_group_address_ids(g2.id),
        }
        balances = (_place_balance(a1.id), _place_balance(a3.id))

        add = fresh.post(f"{API}/place-groups/{g2.id}/addresses",
                         json={"addressIds": [a1.id], "reason": "r"}, headers=headers)
        assert add.status_code == 400, add.get_json()
        assert add.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

        create = fresh.post(f"{API}/place-groups",
                            json={"addressIds": [a1.id, a3.id], "reason": "r"}, headers=headers)
        assert create.status_code == 400, create.get_json()
        assert create.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

        _db.session.expire_all()
        for group_id, expected in members.items():
            assert CustomerLinkService().get_place_group_address_ids(group_id) == expected
        assert (_place_balance(a1.id), _place_balance(a3.id)) == balances
        assert _assert_conserved(before) == []

    def test_adding_to_a_missing_group_is_a_404_not_a_400(self, app, db):
        """The service raises `ValidationError(error_code='PLACE_GROUP_NOT_FOUND')`
        and the ROUTE has a special arm translating exactly that code into a
        404. Rename the code on either side and a missing group becomes a 400 —
        a client cannot tell "you sent nonsense" from "that place is gone".
        """
        _admin_user, headers = _admin(app)
        a1 = _address(_user())
        resp = app.test_client().post(f"{API}/place-groups/999999/addresses",
                                      json={"addressIds": [a1.id], "reason": "r"},
                                      headers=headers)
        assert resp.status_code == 404, resp.get_json()
        assert resp.get_json()["message"] == "PlaceGroup not found"

    def test_a_missing_address_id_in_a_join_is_rejected_before_anything_is_written(
        self, app, db
    ):
        """`_load_addresses` does one IN query and compares lengths, so a
        partial set raises — but `_absorb_joiners_into_group` mutates membership
        and re-stamps ledger rows as its FIRST step. If the load ever moves
        after the absorb, a half-applied join leaves ledger rows stamped to a
        group whose membership write rolled back.
        """
        admin, headers = _admin(app)
        owner_a, owner_b, owner_c = _user(), _user(), _user()
        a1, a2 = _address(owner_a), _address(owner_b)
        group = _group([a1, a2], admin=admin)
        _deliver(owner_a, a1, 7, actor=admin)
        joiner = _address(owner_c)
        _deliver(owner_c, joiner, 3, actor=admin)
        before = _conservation_probe()
        balance_before = _place_balance(a1.id)

        resp = app.test_client().post(f"{API}/place-groups/{group.id}/addresses",
                                      json={"addressIds": [joiner.id, 999999], "reason": "r"},
                                      headers=headers)
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["data"]["error_code"] == "CUSTOMER_LINK_ADDRESS_NOT_FOUND"

        _db.session.expire_all()
        assert UserAddress.query.get(joiner.id).address_group_id is None
        assert _place_balance(a1.id) == balance_before
        assert _place_balance(joiner.id) == Decimal("3.00")
        assert BottleLedger.query.filter(
            BottleLedger.address_id == joiner.id,
            BottleLedger.address_group_id.isnot(None),
        ).count() == 0
        assert _assert_conserved(before) == []


class TestPlaceGroupDetailRoute:
    def test_the_detail_route_404s_and_publishes_numbers_not_decimal_strings(self, app, db):
        """Decimal-as-string is the recurring trap this surface carries two
        separate comments about. And re-introducing a per-member `balance` key
        is the single change that would most mislead an admin — the pool has no
        per-coworker slice.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        owner_a, owner_b = _user(first_name="Ali", last_name="One"), _user(first_name="Bob", last_name="Two")
        a1 = _address(owner_a, title="office")
        a2 = _address(owner_b, title="office")
        group = _group([a1, a2], admin=admin, label="HQ")
        _deliver(owner_a, a1, 6, actor=admin)
        _deliver(owner_b, a2, 5, actor=admin)
        _return(owner_b, a2, 4, actor=admin)

        assert fresh.get(f"{API}/place-groups/999999", headers=headers).status_code == 404

        resp = fresh.get(f"{API}/place-groups/{group.id}", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["place_group_id"] == group.id and data["label"] == "HQ"
        assert isinstance(data["place_balance"], (int, float))
        assert not isinstance(data["place_balance"], bool)
        assert data["place_balance"] == 7.0
        assert [m["address_id"] for m in data["members"]] == sorted([a1.id, a2.id])
        for member in data["members"]:
            assert isinstance(member["suggested_bottles_leaving"], (int, float))
            assert "balance" not in member
            assert set(member["owner"]) == {"id", "first_name", "last_name", "phone"}
            assert "address_title" in member and "full_address" in member
        assert "cod" in data and "events" in data

    def test_the_detail_route_survives_a_group_that_lost_its_last_member(self, app, db):
        """`get_place_group_detail` short-circuits `place_balance` to 0.00 when
        there are no addresses — but only because `addresses[0].id` is guarded.
        An unguarded index is an IndexError -> 500 on EVERY memberless group,
        and the panel must survive being open when the group it was editing
        loses its last member.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        a1, a2 = _address(_user(), title="office"), _address(_user(), title="office")
        group = _group([a1, a2], admin=admin)

        assert fresh.delete(f"{API}/place-groups/{group.id}/addresses/{a2.id}",
                            json={"reason": "moved out"}, headers=headers).status_code == 200
        # The dissolve released the place onto a1 and un-pointed it too, so the
        # group is memberless while its AddressGroup row is deliberately KEPT.
        assert AddressGroup.query.get(group.id) is not None

        resp = fresh.get(f"{API}/place-groups/{group.id}", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["members"] == []
        assert data["place_balance"] == 0.0
        assert {e["event_type"] for e in data["events"]} == {
            "create_place_group", "remove_from_place_group"
        }
        assert any("place dissolved onto its last member" in e["reason"] for e in data["events"])
        assert data["cod"] is not None, "the COD statement must not 500 on an empty member set"

    def test_the_audit_prefix_does_not_leak_a_neighbouring_groups_trail(self, app, db):
        """`CustomerLinkEvent` has NO group column — the `'[group <id>] '` reason
        PREFIX is the scope key. A prefix match on `'[group 1]'` also matches
        `'[group 12]'` unless the closing bracket is included, so two-digit
        group ids in production would leak another place's audit trail.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        groups = []
        for _ in range(12):
            g = _group([_address(_user(), title="o"), _address(_user(), title="o")], admin=admin)
            groups.append(g)
        first, twelfth = groups[0], groups[-1]
        assert twelfth.id > first.id

        for group in (first, twelfth):
            member = CustomerLinkService().get_place_group_address_ids(group.id)[-1]
            assert fresh.delete(f"{API}/place-groups/{group.id}/addresses/{member}",
                                json={"reason": "left"}, headers=headers).status_code == 200

        def event_ids(group_id):
            resp = fresh.get(f"{API}/place-groups/{group_id}", headers=headers)
            assert resp.status_code == 200, resp.get_json()
            return {e["id"] for e in resp.get_json()["data"]["events"]}

        first_ids, twelfth_ids = event_ids(first.id), event_ids(twelfth.id)
        assert first_ids and twelfth_ids

        def reasons(ids):
            return {e.id: e.reason
                    for e in CustomerLinkEvent.query.filter(CustomerLinkEvent.id.in_(ids)).all()}

        # The invariant, asserted per event rather than by count: the '[group N]'
        # token — INCLUDING its closing bracket — is the whole scope key.
        for group, ids in ((first, first_ids), (twelfth, twelfth_ids)):
            for event_id, reason in reasons(ids).items():
                assert reason.startswith(f"[group {group.id}]"), (
                    f"place {group.id}'s trail contains event {event_id}: {reason!r}"
                )
        assert first_ids.isdisjoint(twelfth_ids), (
            f"an audit trail leaked between place {first.id} and place {twelfth.id}: "
            f"{reasons(first_ids & twelfth_ids)}"
        )
        # ...and each place sees exactly its own create + its own removal.
        assert len(first_ids) == len(twelfth_ids) == 2


class TestRemoveRouteBoundary:
    def test_the_reason_guard_runs_BEFORE_the_membership_check(self, app, db):
        """Pinned deliberately. An admin debugging a 400 will add a reason and
        then get a 404 — two round trips for one mistake. More importantly, if
        the membership check ever moves ahead of the reason check, a
        missing-reason request against a VALID member starts 404ing, which reads
        as "the address is gone".
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        g1_a, g1_b = _address(_user(), title="o"), _address(_user(), title="o")
        g2_a, g2_b = _address(_user(), title="p"), _address(_user(), title="p")
        g1 = _group([g1_a, g1_b], admin=admin)
        _group([g2_a, g2_b], admin=admin)

        no_reason = fresh.delete(f"{API}/place-groups/{g1.id}/addresses/{g2_a.id}",
                                 json={}, headers=headers)
        assert no_reason.status_code == 400
        assert "reason is required" in " ".join(no_reason.get_json()["errors"])

        with_reason = fresh.delete(f"{API}/place-groups/{g1.id}/addresses/{g2_a.id}",
                                   json={"reason": "r"}, headers=headers)
        assert with_reason.status_code == 404
        assert with_reason.get_json()["message"] == "PlaceGroupAddress not found"

    def test_removing_an_address_from_a_DIFFERENT_group_leaves_that_group_untouched(
        self, app, db
    ):
        """Without the equality check the service would happily remove the
        address from ITS OWN group while the audit event and the response name
        the other one — an audit trail describing a different place than the one
        that changed.
        """
        admin, headers = _admin(app)
        owner1, owner2, owner3, owner4 = (_user() for _ in range(4))
        a1, a2 = _address(owner1, title="o"), _address(owner2, title="o")
        a3, a4 = _address(owner3, title="p"), _address(owner4, title="p")
        g1 = _group([a1, a2], admin=admin)
        g2 = _group([a3, a4], admin=admin)
        _deliver(owner3, a3, 5, actor=admin)
        before = _conservation_probe()
        events_before = CustomerLinkEvent.query.count()

        resp = app.test_client().delete(f"{API}/place-groups/{g1.id}/addresses/{a3.id}",
                                        json={"reason": "r"}, headers=headers)
        assert resp.status_code == 404, resp.get_json()

        _db.session.expire_all()
        assert UserAddress.query.get(a3.id).address_group_id == g2.id
        assert _place_balance(a3.id) == Decimal("5.00")
        assert CustomerLinkEvent.query.count() == events_before
        assert _assert_conserved(before) == []

    def test_a_delete_body_survives_only_as_json(self, app, db):
        """A DELETE with a body is unusual and easily dropped by a proxy or an
        axios upgrade; the route reads it with `get_json(silent=True) or {}`, so
        a silently-vanished body shows the admin "reason is required" on a form
        where they typed a reason. Only an HTTP-level test catches this.
        """
        admin, headers = _admin(app)
        fresh = app.test_client()
        a1, a2, a3 = (_address(_user(), title="o") for _ in range(3))
        group = _group([a1, a2, a3], admin=admin)
        auth_only = {"Authorization": headers["Authorization"]}

        no_body = fresh.delete(f"{API}/place-groups/{group.id}/addresses/{a3.id}",
                               headers=auth_only)
        assert no_body.status_code == 400
        assert "reason is required" in " ".join(no_body.get_json()["errors"])

        wrong_type = fresh.delete(f"{API}/place-groups/{group.id}/addresses/{a3.id}",
                                  data=json.dumps({"reason": "typed it"}),
                                  content_type="text/plain", headers=auth_only)
        assert wrong_type.status_code == 400
        assert "reason is required" in " ".join(wrong_type.get_json()["errors"])

        _db.session.expire_all()
        assert UserAddress.query.get(a3.id).address_group_id == group.id

        proper = fresh.delete(f"{API}/place-groups/{group.id}/addresses/{a3.id}",
                              data=json.dumps({"reason": "typed it"}),
                              content_type="application/json", headers=auth_only)
        assert proper.status_code == 200, proper.get_json()
        assert proper.get_json()["data"]["bottles_leaving"] == 0.0
        assert isinstance(proper.get_json()["data"]["bottles_leaving"], float)
        assert proper.get_json()["data"]["dissolved"] is False
        assert "netting" not in proper.get_json()["data"], "the §8 netting key is retired"


class TestTheDeleteFenceGuardOrder:
    """`assert_address_not_in_place_group` is returned EARLY, ahead of the bare
    `except Exception` (which would turn it into a 500) and ahead of BOTH the
    only-address and subscription guards. Guard ORDER decides which message the
    admin sees: being told "this is the only address" when the real blocker is
    place membership sends them to delete a different address first — and then
    they hit the real fence anyway.
    """

    def test_the_fence_precedes_the_only_address_guard(self, app, db):
        admin, headers = _admin(app)
        owner = _user()
        only = _address(owner, title="office")
        _group([only, _address(_user(), title="office")], admin=admin)
        assert UserAddress.query.filter_by(user_id=owner.id).count() == 1
        manager, _ = _manager(app)

        resp = app.test_client().delete(
            f"{API}/users/{owner.id}/addresses/{only.id}",
            headers=_headers(app, manager, with_role_claim=True),
        )
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        assert "only address" not in " ".join(resp.get_json()["errors"])
        assert UserAddress.query.get(only.id) is not None

    def test_the_fence_precedes_the_subscription_guard(self, app, db):
        from business_app.models.subscription import Subscription
        from shared.enums import PaymentMethod, SubscriptionFrequency, SubscriptionStatus

        admin, headers = _admin(app)
        owner = _user()
        grouped = _address(owner, title="office")
        _address(owner, title="spare")           # so the only-address guard cannot fire
        _group([grouped, _address(_user(), title="office")], admin=admin)
        subscription = Subscription(
            subscription_number=f"SUB-{next(_SEQ)}",
            user_id=owner.id,
            status=SubscriptionStatus.ACTIVE,
            name="weekly water",
            billing_cycle=SubscriptionFrequency.WEEKLY,
            billing_amount=Decimal("50000.00"),
            next_billing_date=datetime(2026, 12, 1, tzinfo=UTC),
            delivery_frequency=SubscriptionFrequency.WEEKLY,
            delivery_address_id=grouped.id,
            next_delivery_date=datetime(2026, 12, 1, tzinfo=UTC),
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            payment_method=PaymentMethod.CASH,
        )
        _db.session.add(subscription)
        _db.session.commit()
        manager, _ = _manager(app)

        resp = app.test_client().delete(
            f"{API}/users/{owner.id}/addresses/{grouped.id}",
            headers=_headers(app, manager, with_role_claim=True),
        )
        assert resp.status_code == 400, resp.get_json()
        assert resp.get_json()["data"]["error_code"] == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
        assert "subscription" not in " ".join(resp.get_json()["errors"]).lower()
        assert UserAddress.query.get(grouped.id) is not None

    def test_an_ungrouped_address_still_deletes_normally(self, app, db):
        """`assert_address_not_in_place_group` reads a row and checks
        `row[0] is not None`. An `if row:` truthiness slip would fence EVERY
        address (a one-tuple is truthy) and make address deletion impossible
        platform-wide.
        """
        owner = _user()
        keep, doomed = _address(owner, title="home"), _address(owner, title="spare")
        manager, _ = _manager(app)

        resp = app.test_client().delete(
            f"{API}/users/{owner.id}/addresses/{doomed.id}",
            headers=_headers(app, manager, with_role_claim=True),
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["message"] == "Address deleted successfully"
        assert UserAddress.query.get(doomed.id) is None
        assert UserAddress.query.get(keep.id) is not None


class TestTheLegacyCanonicalCustomerRoute:
    def test_the_legacy_route_still_works_and_differs_from_place_groups(self, app, db):
        """PINNED. Two creation paths with DIFFERENT audit guarantees. A place
        created through `/canonical-customers/<id>/address-groups` has an EMPTY
        reason on its audit event and cannot carry a §7.4 merge review — so a
        drifted place formed through this route can never be converged and the
        Reconcile button remains able to destroy its balance. The echoed
        `address_ids` also come from the REQUEST, not from the created group.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        a1, a2 = _address(_user(), title="o"), _address(_user(), title="o")

        created = fresh.post(f"{API}/canonical-customers/4242/address-groups",
                             json={"addressIds": [a2.id, a1.id], "label": "Office",
                                   "resultingBalance": 99},
                             headers=headers)
        assert created.status_code == 201, created.get_json()
        data = created.get_json()["data"]
        group_id = data["address_group_id"]
        assert data["address_ids"] == sorted([a1.id, a2.id])
        assert "place_group_id" not in data, "the legacy route uses the older key name"

        # No reason -> an audit event with an unexplained reason.
        event = CustomerLinkEvent.query.filter(
            CustomerLinkEvent.reason.like(f"[group {group_id}]%")
        ).one()
        assert event.reason == f"[group {group_id}]"
        assert event.event_metadata.get("resulting_balance") is None, (
            "the merge review is NOT forwarded by the legacy route"
        )
        # ...and the accompanying resultingBalance was silently ignored.
        assert _place_balance(a1.id) == Decimal("0.00")

        single = fresh.post(f"{API}/canonical-customers/4242/address-groups",
                            json={"addressIds": [_address(_user()).id]}, headers=headers)
        assert single.status_code == 400, single.get_json()
        assert single.get_json()["data"]["error_code"] == "PLACE_GROUP_MIN_ADDRESSES"

        empty = fresh.post(f"{API}/canonical-customers/4242/address-groups",
                           json={"addressIds": []}, headers=headers)
        assert empty.status_code == 400
        assert "addressIds is required" in " ".join(empty.get_json()["errors"])


# =========================================================================== #
# 14. THE TWO ERROR ENVELOPES
# =========================================================================== #


class TestErrorEnvelopeShapes:
    def test_error_code_lives_at_two_DIFFERENT_json_paths(self, app, db, world):
        """A client branching on `error_code` must know it lives at two
        different paths, and the prose that carries the ACTUAL cap
        (`PLACE_SPLIT_INVALID`'s "must be between 0 and the place balance
        (7.00)") sits at a third. BottleTracking.js displays
        `err.response.data.error` — on a bottle route that is the literal string
        'VALIDATION_ERROR'; LinkedAccountsPanel.jsx displays
        `err.response.data.message` — on a place route that is the literal
        'Validation failed'. So no admin surface currently shows the real reason.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()

        # Bottle family: handle_api_exception -> ErrorResponse.build_error_response
        _deliver(world["a"], world["a1"], 1, actor=world["admin"])
        bottle = fresh.post(f"{API}/bottles/initial-balance",
                            json={"address_id": world["a1"].id, "quantity": 4},
                            headers=headers)
        assert bottle.status_code == 400
        body = bottle.get_json()
        assert body["error"] == "VALIDATION_ERROR"
        assert body["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"
        assert body["status_code"] == 400
        assert "already has a bottle balance" in body["message"]
        assert "success" not in body and "errors" not in body

        # Place family: validation_error_response -> error_response
        place = fresh.delete(f"{API}/place-groups/{world['group'].id}/addresses/{world['a2'].id}",
                             json={"reason": "r", "bottlesLeaving": 999}, headers=headers)
        assert place.status_code == 400
        body = place.get_json()
        assert body["success"] is False
        assert body["message"] == "Validation failed"
        assert body["data"]["error_code"] == "PLACE_SPLIT_INVALID"
        assert any("between 0 and the place balance" in e for e in body["errors"])
        assert "error" not in body, "the place family emits NO top-level `error` key"
        assert "error_code" not in body, "on the place family error_code is nested under data"

    def test_the_two_admin_ui_error_readers_both_miss_the_real_reason(self, app, db, world):
        """Pinned against the components themselves. Neither surface shows the
        prose that names the cap, so an admin refused a split is told
        'VALIDATION_ERROR' or 'Validation failed'. Fixing either component must
        flip this test deliberately.
        """
        root = Path(__file__).resolve().parents[2] / "admin_ui/src"
        bottle_page = (root / "pages/BottleTracking.js").read_text()
        linked_panel = (root / "components/LinkedAccountsPanel.jsx").read_text()
        assert "response?.data?.error" in bottle_page or "response.data.error" in bottle_page
        assert "response?.data?.message" in linked_panel or "response.data.message" in linked_panel

        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        refused = fresh.delete(
            f"{API}/place-groups/{world['group'].id}/addresses/{world['a2'].id}",
            json={"reason": "r", "bottlesLeaving": 999}, headers=headers,
        ).get_json()
        # What LinkedAccountsPanel would render, versus what the admin needs.
        assert refused["message"] == "Validation failed"
        assert refused.get("error") is None
        assert "7.00" in " ".join(refused["errors"]), (
            "the only place the real cap appears is the `errors` array nobody reads"
        )


# =========================================================================== #
# 15. THE FULL JOURNEY, DRIVEN ONLY BY representative_address_id
# =========================================================================== #


class TestTheFullAdminJourneyOnASharedPlace:
    def test_every_write_route_accepts_the_representative_id_and_operates_on_the_PLACE(
        self, app, db, world
    ):
        """A grouped balance row has `address_id` NULL by CHECK constraint, so
        `representative_address_id` is the ONLY id the admin UI has for a shared
        place. If any single route stops expanding an address to its place — or
        if the representative rule and the derivation rule drift apart — the
        drawer opens the right place and the write books a different one, with
        no error at all.
        """
        _admin_user, headers = _admin(app)
        fresh = app.test_client()
        row = next(r for r in _balances(fresh, headers)["items"]
                   if r["address_group_id"] == world["group"].id)
        assert row["address_id"] is None
        place_id = row["representative_address_id"]
        assert place_id is not None
        other_id = next(a for a in row["member_address_ids"] if a != place_id)

        results = {}
        for label, address_id in (("representative", place_id), ("other", other_id)):
            _db.session.expire_all()
            balance_before = _place_balance(address_id)

            ledger = fresh.get(f"{API}/bottles/ledger/{address_id}", headers=headers)
            assert ledger.status_code == 200, ledger.get_json()

            adjust = fresh.post(f"{API}/bottles/adjustment",
                                json={"address_id": address_id, "adjustment": 2, "notes": label},
                                headers=headers)
            assert adjust.status_code == 200, adjust.get_json()
            assert adjust.get_json()["data"]["address_group_id"] == world["group"].id

            initial = fresh.post(f"{API}/bottles/initial-balance",
                                 json={"address_id": address_id, "quantity": 1}, headers=headers)
            assert initial.status_code == 400, initial.get_json()
            assert initial.get_json()["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"

            fine = fresh.post(f"{API}/bottles/fines",
                              json={"address_id": address_id, "quantity": 1,
                                    "fine_amount": 12000},
                              headers=headers)
            assert fine.status_code == 200, fine.get_json()
            assert fine.get_json()["data"]["address_group_id"] == world["group"].id

            reconcile = fresh.post(f"{API}/bottles/reconcile/{address_id}", headers=headers)
            assert reconcile.status_code == 200, reconcile.get_json()
            assert reconcile.get_json()["data"]["address_group_id"] == world["group"].id
            assert reconcile.get_json()["data"]["address_id"] is None

            _db.session.expire_all()
            results[label] = {
                "ledger_ids": {e["id"] for e in ledger.get_json()["data"]["items"]},
                "moved": _place_balance(address_id) - balance_before,
                "scope": reconcile.get_json()["data"]["address_group_id"],
            }

        assert results["representative"]["moved"] == results["other"]["moved"] == Decimal("2")
        assert results["representative"]["scope"] == results["other"]["scope"] == world["group"].id
        # EXACT, not a bare superset: the second member's drawer must show the
        # first member's whole history PLUS precisely the two rows the first
        # iteration appended (one ADMIN_ADJUSTMENT, one FINE_ISSUED). A superset
        # assertion alone would also pass if the second read had leaked rows from
        # another place.
        added = results["other"]["ledger_ids"] - results["representative"]["ledger_ids"]
        assert results["representative"]["ledger_ids"] < results["other"]["ledger_ids"]
        assert len(added) == 2, added
        assert {
            BottleLedger.query.get(i).event_type for i in added
        } == {BottleLedgerEventType.ADMIN_ADJUSTMENT, BottleLedgerEventType.FINE_ISSUED}
        assert all(
            BottleLedger.query.get(i).address_group_id == world["group"].id
            for i in results["other"]["ledger_ids"]
        )

    def test_the_dashboard_counts_a_shared_place_as_ONE_debtor(self, app, db, world):
        """Two coworkers sharing an office are ONE debtor holding one pool, not
        a 6/1 split. `top_debtors` rows are places, and `_scope_label` is what
        gives an ownerless row an identity.
        """
        _admin_user, headers = _admin(app)
        data = app.test_client().get(f"{API}/bottles/dashboard", headers=headers
                                     ).get_json()["data"]

        office = [d for d in data["top_debtors"] if d["address_group_id"] == world["group"].id]
        assert len(office) == 1
        assert office[0]["name"] == "Acme office"
        assert office[0]["total_balance"] == 7.0
        assert office[0]["address_id"] is None
        assert data["places_with_balance"] == BottleBalance.query.filter(
            BottleBalance.balance > 0).count()
        assert data["total_bottles_out"] == float(
            sum(Decimal(str(b.balance)) for b in BottleBalance.query.filter(
                BottleBalance.balance > 0).all())
        )
        assert data["active_fines"] == 1
        assert data["total_fine_amount"] == 50000.0


# =========================================================================== #
# 15B. THE GAP HUNT — compositions the per-axis split structurally missed
#
# Every scenario below satisfies GLOBAL conservation AND per-place
# `get_place_balance == ledger_sum` for every scope it touches. That is exactly
# why nothing already in this file sees them: the damage is that bottles (or a
# customer's history, or a whole second pool) become UNREACHABLE from the
# identifier the admin was handed — not that anything is minted or destroyed.
# Every assertion here is therefore PER SCOPE and names WHICH identifier
# resolves to WHICH place.
# =========================================================================== #


def _office_of_three(app, *, label="Third-floor office"):
    """A three-member place holding 7.00, whose LOWEST-id member is the id the
    balances list publishes as `representative_address_id`.

    Three members and not two on purpose: removing one member from a TWO-member
    group dissolves the place onto the survivor (§7.3), which re-stamps the
    survivor's rows and deletes the group row — a different scenario entirely.
    Three keeps the place alive so "the handle went stale" is the only thing
    that changed.
    """
    admin, headers = _admin(app)
    owner_a = _user(first_name="Ann", last_name="Anvarova")
    owner_b = _user(first_name="Bek", last_name="Bekov")
    owner_c = _user(first_name="Cem", last_name="Cemilov")
    a = _address(owner_a, title="desk-a")
    b = _address(owner_b, title="desk-b")
    c = _address(owner_c, title="desk-c")
    group = _group([a, b, c], admin=admin, label=label)
    _deliver(owner_a, a, 6, actor=admin)
    _deliver(owner_b, b, 5, actor=admin)
    _return(owner_b, b, 4, actor=admin)
    assert a.id < b.id < c.id
    return {
        "admin": admin,
        "headers": headers,
        "group_id": group.id,
        "a_id": a.id,
        "b_id": b.id,
        "c_id": c.id,
        "owner_a_id": owner_a.id,
        "owner_b_id": owner_b.id,
    }


def _remove_member(app, headers, group_id, address_id, *, reason="moved out"):
    """The real admin removal route. Asserts the place did NOT dissolve."""
    resp = app.test_client().delete(
        f"{API}/place-groups/{group_id}/addresses/{address_id}",
        json={"reason": reason}, headers=headers,
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["dissolved"] is False, (
        "this scenario needs the place to survive the removal"
    )
    _db.session.expire_all()
    return resp.get_json()["data"]


class TestTheServedRepresentativeIdBecomesOnePersonsPrivateHandle:
    """`GET /admin/bottles/balances` publishes `representative_address_id` as
    THE write handle for a place — a grouped row's `address_id` is NULL by CHECK
    constraint, so it is the only id the panel has. Nothing revalidates that id
    against the place when the write arrives: `resolve_scope` reads
    `addresses.address_group_id` as it is at write time, so once that member has
    left, every "place" write posted at the handle the SERVER served lands in
    that one person's private scope instead.

    This is not the already-pinned "a delivery after a removal goes to the
    departed address" case: there the client chose the address. Here the client
    was HANDED the id as the place's identity and had no way to learn it went
    stale. The admin sees the place unchanged, retries, and double-writes a
    departed customer.

    TODAY'S BEHAVIOUR IS PINNED, NOT xfailed. The one defence that exists is
    real and is asserted here as a fence: all three write responses echo the
    scope they actually wrote to (`address_group_id`), so a panel that compared
    it against the row it rendered could detect the retarget. Drop that key from
    `to_dict` and the retarget becomes completely undetectable from the client.
    A fix — a refusal, an If-Match-style scope token, or the panel comparing the
    echo — MUST change the numbers pinned below.
    """

    def test_an_adjustment_at_the_served_handle_moves_a_departed_members_scope_not_the_place(
        self, app, db
    ):
        w = _office_of_three(app)
        fresh = app.test_client()
        row = next(r for r in _balances(fresh, w["headers"])["items"]
                   if r["address_group_id"] == w["group_id"])
        handle = row["representative_address_id"]
        assert handle == w["a_id"], "the panel is served the lowest-id member"
        assert row["balance"] == 7.0
        assert row["address_id"] is None

        # The place drawer is open; the admin has not refetched. Meanwhile:
        _remove_member(app, w["headers"], w["group_id"], handle)

        before = _conservation_probe()
        resp = fresh.post(
            f"{API}/bottles/adjustment",
            json={"address_id": handle, "adjustment": 4, "notes": "stock count"},
            headers=w["headers"],
        )
        assert resp.status_code == 200, resp.get_json()
        _db.session.expire_all()

        # (1) GLOBAL conservation is untouched — the oracle sees nothing at all.
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("4")]

        # (2) PER SCOPE: the place the admin was looking at never moved...
        assert _place_balance(w["b_id"]) == Decimal("7.00")
        assert _place_balance(w["c_id"]) == Decimal("7.00")
        assert _ledger_sum(BottleScope.for_group(w["group_id"])) == Decimal("7.00")
        # ...and the 4 bottles are now one departed person's private balance.
        assert _place_balance(handle) == Decimal("4.00")
        assert _ledger_sum(BottleScope.for_address(handle)) == Decimal("4.00")
        assert appended[0].address_group_id is None
        assert appended[0].user_id == w["owner_a_id"], (
            "attributed to the departed member's owner, who is no longer at the place"
        )

        # (3) The response DOES say where it landed — the panel's only defence.
        data = resp.get_json()["data"]
        assert data["address_id"] == handle
        assert data["address_group_id"] is None, (
            "the scope echo is the only signal a client can use to detect the "
            "retarget; the row the panel rendered said address_group_id="
            f"{row['address_group_id']}"
        )

        # (4) The drawer the panel opens with the same stale handle, under the
        #     place's label, now shows ONE row instead of the place's three.
        drawer = fresh.get(f"{API}/bottles/ledger/{handle}", headers=w["headers"])
        assert drawer.status_code == 200, drawer.get_json()
        assert [i["id"] for i in drawer.get_json()["data"]["items"]] == [appended[0].id]

        # (5) One physical office is now two rows on the balances screen.
        after = _balances(fresh, w["headers"])["items"]
        place_row = next(r for r in after if r["address_group_id"] == w["group_id"])
        assert place_row["balance"] == 7.0
        assert handle not in place_row["member_address_ids"]
        assert place_row["representative_address_id"] == w["b_id"], (
            "a refetch would have served a DIFFERENT handle — the panel had no "
            "way to know the one it held expired"
        )
        departed_row = next(r for r in after if r["address_id"] == handle)
        assert departed_row["balance"] == 4.0
        assert departed_row["is_shared_place"] is False

    def test_the_one_shot_initial_balance_at_the_served_handle_is_burned_on_the_DEPARTED_member(
        self, app, db
    ):
        """`set_initial_balance`'s "this place has no history yet" guard is
        SCOPE-LOCAL: it asks the post-departure scope, which is empty, while the
        address itself demonstrably has a ledger row (stamped to the former
        group, per §7.1). So the guard passes, the opening balance is accepted,
        and it opens a private pool for someone who has left.
        """
        w = _office_of_three(app)
        fresh = app.test_client()
        handle = next(r for r in _balances(fresh, w["headers"])["items"]
                      if r["address_group_id"] == w["group_id"])["representative_address_id"]
        _remove_member(app, w["headers"], w["group_id"], handle)

        # The address is NOT historyless: its delivery is still on the books,
        # stamped with the group it has left.
        own_rows = BottleLedger.query.filter_by(address_id=handle).all()
        assert [r.address_group_id for r in own_rows] == [w["group_id"]]

        before = _conservation_probe()
        resp = fresh.post(f"{API}/bottles/initial-balance",
                          json={"address_id": handle, "quantity": 12},
                          headers=w["headers"])
        assert resp.status_code == 200, resp.get_json()
        _db.session.expire_all()

        data = resp.get_json()["data"]
        assert data["address_group_id"] is None
        assert data["idempotency_key"] is None, (
            "UPDATED: `set_initial_balance` no longer carries an idempotency key "
            "at all — see TestInitialBalanceRoute. What this test is about is "
            "unchanged: the has-history guard is SCOPE-LOCAL, so a departed "
            "address whose ledger rows are stamped to the group it left still "
            "reads as historyless."
        )
        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("12")]

        assert _place_balance(handle) == Decimal("12.00")
        assert _place_balance(w["b_id"]) == Decimal("7.00"), "the office never moved"
        assert {r.address_group_id for r in BottleLedger.query.filter_by(address_id=handle).all()} == {
            w["group_id"], None,
        }

        # The PLACE's own one-shot is untouched — so nothing about this write is
        # recoverable by repeating it at the handle a refetch would now serve.
        second = fresh.post(f"{API}/bottles/initial-balance",
                            json={"address_id": w["b_id"], "quantity": 12},
                            headers=w["headers"])
        assert second.status_code == 400, second.get_json()
        assert second.get_json()["error_code"] == "BOTTLE_INITIAL_BALANCE_EXISTS"

    def test_a_fine_at_the_served_handle_freezes_to_the_departed_members_scope_forever(
        self, app, db
    ):
        """A fine's scope is FROZEN at issue (models/bottle.py:175-179), so a
        write that retargets at issue time can never be re-pointed at the place:
        the FINE_ISSUED/FINE_PAID pair lives and dies in the departed member's
        private scope, and the shortage the fine records is judged against 0.00
        rather than the 7.00 the admin was looking at.
        """
        w = _office_of_three(app)
        fresh = app.test_client()
        handle = next(r for r in _balances(fresh, w["headers"])["items"]
                      if r["address_group_id"] == w["group_id"])["representative_address_id"]
        _remove_member(app, w["headers"], w["group_id"], handle)

        issued = fresh.post(f"{API}/bottles/fines",
                            json={"address_id": handle, "quantity": 2, "fine_amount": 30000},
                            headers=w["headers"])
        assert issued.status_code == 200, issued.get_json()
        _db.session.expire_all()
        fine_id = issued.get_json()["data"]["id"]
        assert issued.get_json()["data"]["address_group_id"] is None
        assert issued.get_json()["data"]["user_id"] == w["owner_a_id"]

        issued_row = BottleLedger.query.filter_by(
            event_type=BottleLedgerEventType.FINE_ISSUED
        ).one()
        assert issued_row.address_group_id is None
        assert issued_row.entry_metadata["place_balance_at_issue"] == 0.0, (
            "the shortage context recorded for the fine is the departed "
            "member's empty scope, not the 7.00 the admin's screen showed"
        )

        # The fines list labels it by the departed address, never as the office.
        listed = fresh.get(f"{API}/bottles/fines", headers=w["headers"]).get_json()["data"]
        fine_row = next(f for f in listed["items"] if f["id"] == fine_id)
        assert fine_row["place_label"] == "desk-a"
        assert fine_row["address_group_id"] is None

        before = _conservation_probe()
        paid = fresh.put(f"{API}/bottles/fines/{fine_id}",
                         json={"action": "mark_paid"}, headers=w["headers"])
        assert paid.status_code == 200, paid.get_json()
        _db.session.expire_all()

        appended = _assert_conserved(before)
        assert [Decimal(str(e.quantity)) for e in appended] == [Decimal("-2")]
        assert appended[0].address_group_id is None
        assert _place_balance(handle) == Decimal("-2.00")
        assert _place_balance(w["b_id"]) == Decimal("7.00")
        assert _ledger_sum(BottleScope.for_group(w["group_id"])) == Decimal("7.00")


# --------------------------------------------------------------------------- #
# A departed member's history: reachable BY PERSON, invisible BY ADDRESS
# --------------------------------------------------------------------------- #


class TestADepartedMembersHistoryIsReachableOnlyByPERSON:
    """§7.1's rule — a departed address's rows stay stamped with its FORMER
    group — is deliberate and load-bearing: it is what stops a re-join dragging
    a whole place's history back. Its effect on the ADMIN LEDGER SCREEN is not
    written down anywhere, and it is severe: the same three rows are returned by
    the person filter, returned by any REMAINING member's address filter, and
    returned by NEITHER address-keyed read on the customer whose bottles they
    are.

    An operator investigating a disputed count opens the departed customer's
    address, is told "no bottle history", and closes the dispute. Every existing
    scenario about this filter uses a CURRENT member.
    """

    @staticmethod
    def _departed(app):
        w = _office_of_three(app, label="Fourth-floor office")
        # All three movements happen at A's door while A is a member.
        _deliver(User.query.get(w["owner_a_id"]), UserAddress.query.get(w["a_id"]), 5,
                 actor=User.query.get(w["admin"].id))
        _remove_member(app, w["headers"], w["group_id"], w["a_id"])
        return w

    def test_the_address_filter_reads_EMPTY_while_the_person_filter_returns_every_row(
        self, app, db
    ):
        w = self._departed(app)
        fresh = app.test_client()

        def ids(query):
            resp = fresh.get(f"{API}/bottles/ledger{query}", headers=w["headers"])
            assert resp.status_code == 200, resp.get_json()
            return {e["id"] for e in resp.get_json()["data"]["items"]}

        by_person = ids(f"?user_id={w['owner_a_id']}")
        rows = BottleLedger.query.filter_by(user_id=w["owner_a_id"]).all()
        assert len(rows) == 2 and by_person == {r.id for r in rows}
        assert {r.address_group_id for r in rows} == {w["group_id"]}, (
            "the departed member's rows keep the FORMER group stamp (§7.1)"
        )
        assert sum(Decimal(str(r.quantity)) for r in rows) == Decimal("11")

        # 1. the departed address, global ledger + address filter -> EMPTY
        assert ids(f"?address_id={w['a_id']}") == set()
        # 2. the departed address, place ledger -> EMPTY
        place = fresh.get(f"{API}/bottles/ledger/{w['a_id']}", headers=w["headers"])
        assert place.status_code == 200, place.get_json()
        assert place.get_json()["data"]["items"] == []
        assert place.get_json()["data"]["total"] == 0
        # 3. a REMAINING member's address filter -> the departed member's rows
        assert by_person <= ids(f"?address_id={w['b_id']}")
        # 4. ...and the place ledger at that remaining member shows them too
        sibling = fresh.get(f"{API}/bottles/ledger/{w['b_id']}", headers=w["headers"])
        assert by_person <= {i["id"] for i in sibling.get_json()["data"]["items"]}

    def test_the_departed_customers_own_summary_screen_reads_zero_while_the_place_holds_the_bottles(
        self, app, db
    ):
        """The fifth read an operator would try. `get_customer_summary` is
        address-keyed too, so it agrees with the two empty reads above and
        disagrees with the person-filtered ledger — the customer's screen says
        0.00 while 11 bottles of their movements sit at a place they left.
        """
        w = self._departed(app)
        fresh = app.test_client()

        summary = fresh.get(f"{API}/bottles/balances/{w['owner_a_id']}", headers=w["headers"])
        assert summary.status_code == 200, summary.get_json()
        data = summary.get_json()["data"]
        assert [a["address_id"] for a in data["addresses"]] == [w["a_id"]]
        assert data["addresses"][0]["place_balance"] == 0.0
        assert data["addresses"][0]["is_grouped"] is False
        assert data["addresses"][0]["address_group_id"] is None
        assert data["cluster_scopes"] == [], (
            "no balance row resolves to the departed address at all"
        )

        # ...while the place still holds everything, at both surviving members.
        assert _place_balance(w["b_id"]) == Decimal("12.00")
        assert _place_balance(w["c_id"]) == Decimal("12.00")
        assert _ledger_sum(BottleScope.for_group(w["group_id"])) == Decimal("12.00")


# --------------------------------------------------------------------------- #
# A memberless place row: published, listed, and un-actionable
# --------------------------------------------------------------------------- #


def _place_address_id_of(record):
    """`placeAddressIdOf` from admin_ui/src/pages/BottleTracking.js:221, in
    Python: `record.address_id ?? record.representative_address_id`."""
    address_id = record.get("address_id")
    return address_id if address_id is not None else record.get("representative_address_id")


class TestAMemberlessPlaceRowIsListedWithNoWorkingAction:
    """The SURFACE consequence of the fine-frozen-scope defects pinned above,
    and it survives whatever shape their fix takes: any path that leaves a
    `bottle_balances` group row without members produces an admin table row that
    looks entirely normal and whose every button is a broken request.

    `_scope_member_address_ids` returns `[]`, so the serializer emits
    `representative_address_id: null` — and `placeAddressIdOf` is
    `address_id ?? representative_address_id`, both null. The four address-keyed
    routes the row's actions post to therefore receive `undefined`.

    Today the row is neither excluded from the list nor flagged. A fix must
    change what this test pins: either the row does not appear, or it carries an
    explicit actionability signal the component can honour.
    """

    @staticmethod
    def _memberless_place(app):
        """Reached only through production routes: a fine issued at a live
        place, then the place dissolved, then the fine settled — `FINE_PAID`
        writes to the FROZEN group scope and `get_or_create_balance` RECREATES
        the row the dissolve deleted.
        """
        admin, headers = _admin(app)
        owner_a, owner_b = _user(first_name="Dil"), _user(first_name="Eld")
        a1, a2 = _address(owner_a, title="office"), _address(owner_b, title="office")
        group = _group([a1, a2], admin=admin)
        _deliver(owner_a, a1, 6, actor=admin)
        fine = BottleTrackingService().issue_fine(
            user_id=None, address_id=a1.id, quantity=Decimal("2"),
            fine_amount=Decimal("30000"), actor_user_id=admin.id,
        )
        fine_id = fine.id
        dissolved = app.test_client().delete(
            f"{API}/place-groups/{group.id}/addresses/{a2.id}",
            json={"reason": "moved out"}, headers=headers,
        )
        assert dissolved.status_code == 200, dissolved.get_json()
        assert dissolved.get_json()["data"]["dissolved"] is True
        paid = app.test_client().put(f"{API}/bottles/fines/{fine_id}",
                                     json={"action": "mark_paid"}, headers=headers)
        assert paid.status_code == 200, paid.get_json()
        _db.session.expire_all()
        if BottleBalance.query.filter_by(address_group_id=group.id).first() is None:
            pytest.skip(
                "the frozen-scope defect is fixed — see "
                "TestFineFrozenScopeOutlivesItsBalanceRow's xfails"
            )
        return admin, headers, group.id, a1.id, owner_a.id

    def test_every_row_action_fires_at_a_null_id_and_fails_in_three_different_ways(
        self, app, db
    ):
        admin, headers, group_id, survivor_id, owner_a_id = self._memberless_place(app)
        fresh = app.test_client()
        record = next(r for r in _balances(fresh, headers)["items"]
                      if r["address_group_id"] == group_id)
        assert _place_address_id_of(record) is None, (
            "if this ever stops being None the four calls below stop being the "
            "requests the panel actually makes"
        )
        assert record["member_address_ids"] == []
        assert record["representative_address_id"] is None

        before = _conservation_probe()
        balance_before = Decimal(str(
            BottleBalance.query.filter_by(address_group_id=group_id).one().balance
        ))

        # The two template-literal routes. `${undefined}` is the literal string
        # the panel sends; `<int:address_id>` cannot match it, so these never
        # reach handle_api_exception and never produce the API error envelope.
        for url in (f"{API}/bottles/ledger/undefined", f"{API}/bottles/reconcile/undefined"):
            method = fresh.get if "/ledger/" in url else fresh.post
            resp = method(url, headers=headers)
            assert resp.status_code == 404, f"{url} -> {resp.status_code}"

        # The three modal writes. `prefillPlaceWriteForm` sets
        # `address_id: undefined`, which JSON.stringify DROPS from the body, so
        # each is a pydantic "Field required" naming the CAMEL alias the panel's
        # form does not use.
        for url, body in (
            (f"{API}/bottles/adjustment", {"adjustment": 4, "notes": "stock count"}),
            (f"{API}/bottles/initial-balance", {"quantity": 4}),
            (f"{API}/bottles/fines", {"quantity": 1, "fine_amount": 1000}),
        ):
            resp = fresh.post(url, json=body, headers=headers)
            assert resp.status_code == 400, f"{url} -> {resp.status_code} {resp.get_json()}"
            prose = " ".join(resp.get_json()["errors"])
            assert "addressId" in prose and "Field required" in prose, f"{url} -> {prose}"

        # Five failed actions, nothing written anywhere.
        _db.session.expire_all()
        assert _assert_conserved(before) == []
        assert Decimal(str(
            BottleBalance.query.filter_by(address_group_id=group_id).one().balance
        )) == balance_before

    def test_the_memberless_row_is_listed_but_unreachable_from_every_customer_filter(
        self, app, db
    ):
        """The row has no members, so no name, phone or user id selects it: the
        only screen it appears on is the unfiltered list, sorted by balance
        beside real places. An operator who notices the number cannot get from
        it to a customer, and a customer-first search can never find it.
        """
        admin, headers, group_id, survivor_id, owner_a_id = self._memberless_place(app)
        fresh = app.test_client()

        unfiltered = _balances(fresh, headers)["items"]
        assert any(r["address_group_id"] == group_id for r in unfiltered), (
            "it is NOT excluded from the list"
        )
        row = next(r for r in unfiltered if r["address_group_id"] == group_id)
        assert row["place_label"] == f"Place #{group_id}"
        assert row["member_names"] == []
        assert "is_actionable" not in row, (
            "nothing on the row tells the panel its action handle is null"
        )

        by_user = _balances(fresh, headers, f"?user_id={owner_a_id}")["items"]
        assert [r["address_group_id"] for r in by_user] == [None], (
            "the surviving member's own filter returns only their own place"
        )
        by_search = _balances(fresh, headers, "?search=Dil")["items"]
        assert all(r["address_group_id"] != group_id for r in by_search)

        # ...and the same row is what the dissolved group's detail route now
        # describes: a place with no members at all.
        detail = fresh.get(f"{API}/place-groups/{group_id}", headers=headers)
        assert detail.status_code == 200, detail.get_json()
        assert detail.get_json()["data"]["members"] == []


# --------------------------------------------------------------------------- #
# A place can never GROW: the suggestion engine cannot see a grouped neighbour
# --------------------------------------------------------------------------- #


class TestAnExistingPlaceCanNeverGrowThroughSuggestions:
    """`_ungrouped_individual_address_query` filters `address_group_id IS NULL`,
    so an existing place's members are INVISIBLE to the suggestion engine. A
    third coworker who registers at the same physical point is therefore alone
    at that point, `candidate_points` requires >= 2 DISTINCT users, and no
    suggestion is ever produced for the place that is already there.

    Offices grow one hire at a time — this is the commonest way a place changes
    after it is created — so the divergence is permanent and silent. Every
    existing suggestion scenario builds ungrouped fixtures, which makes the
    engine's blindness to grouped neighbours invisible by construction.

    A fix must change what these tests pin.
    """

    @staticmethod
    def _office_plus_newcomer(app):
        admin, headers = _admin(app)
        owner_1 = _user(first_name="Gulnora", last_name="Rashidova")
        owner_2 = _user(first_name="Hasan", last_name="Hasanov")
        owner_3 = _user(first_name="Iroda", last_name="Islomova")
        a1 = _address(owner_1, title="reception")
        a2 = _address(owner_2, title="reception")
        group = _group([a1, a2], admin=admin, label="Ground-floor reception")
        _deliver(owner_1, a1, 6, actor=admin)
        _deliver(owner_2, a2, 5, actor=admin)
        _return(owner_2, a2, 4, actor=admin)
        a3 = _address(owner_3, title="reception")   # same coordinates, same text
        assert (a3.latitude, a3.longitude) == (a1.latitude, a1.longitude)
        assert a3.full_address == a1.full_address
        return {
            "admin": admin, "headers": headers, "group_id": group.id,
            "a1_id": a1.id, "a2_id": a2.id, "a3_id": a3.id,
            "owner_1": owner_1, "owner_3": owner_3,
        }

    def test_a_newcomer_at_an_already_grouped_point_is_never_suggested_for_that_place(
        self, app, db
    ):
        w = self._office_plus_newcomer(app)
        service = CustomerLinkService()
        fresh = app.test_client()

        assert service.get_place_group_suggestions() == []
        assert service.get_place_group_suggestions(user_id=w["owner_3"].id) == []
        assert service.get_place_group_suggestions(user_id=w["owner_1"].id) == []

        for anchor in (w["owner_3"], w["owner_1"]):
            resp = fresh.get(f"{API}/users/{anchor.id}/place-group-suggestions",
                             headers=w["headers"])
            assert resp.status_code == 200, resp.get_json()
            assert resp.get_json()["data"]["suggestions"] == [], (
                f"anchor {anchor.id} was offered a suggestion — the engine's "
                "blindness to grouped neighbours has changed"
            )

        # The MANUAL picker can see the newcomer perfectly well: the data is
        # there, only the engine that would surface the need is blind.
        picker = fresh.get(f"{API}/addresses/search?q=reception", headers=w["headers"])
        assert picker.status_code == 200, picker.get_json()
        assert [a["address_id"] for a in picker.get_json()["data"]["addresses"]] == [w["a3_id"]]

    def test_the_second_pool_that_blindness_opens_is_two_admin_rows_for_one_physical_address(
        self, app, db
    ):
        w = self._office_plus_newcomer(app)
        fresh = app.test_client()
        _deliver(w["owner_3"], UserAddress.query.get(w["a3_id"]), 5, actor=w["admin"])
        _db.session.expire_all()

        rows = _balances(fresh, w["headers"])["items"]
        office = next(r for r in rows if r["address_group_id"] == w["group_id"])
        newcomer = next(r for r in rows if r["address_id"] == w["a3_id"])
        assert office["balance"] == 7.0
        assert newcomer["balance"] == 5.0
        assert newcomer["is_shared_place"] is False
        assert newcomer["full_address"] == UserAddress.query.get(w["a1_id"]).full_address, (
            "two rows, two numbers, one physical reception desk"
        )
        assert w["a3_id"] not in office["member_address_ids"]

        # Per scope both are internally perfect — this is why no conservation or
        # stored-vs-ledger oracle anywhere can see the split.
        assert _place_balance(w["a1_id"]) == _ledger_sum(BottleScope.for_group(w["group_id"]))
        assert _place_balance(w["a3_id"]) == _ledger_sum(BottleScope.for_address(w["a3_id"]))

        dash = fresh.get(f"{API}/bottles/dashboard", headers=w["headers"]).get_json()["data"]
        assert dash["places_with_balance"] == 2
        assert dash["total_bottles_out"] == 12.0
        debtors = [d for d in dash["top_debtors"]]
        assert {d["address_group_id"] for d in debtors} == {w["group_id"], None}, (
            "one office counted as two debtor places"
        )

    def test_a_fourth_coworker_resurfaces_a_suggestion_to_build_a_SECOND_place_beside_the_first(
        self, app, db
    ):
        """The engine is not merely quiet — once TWO ungrouped coworkers stand
        at the point it actively proposes a NEW group there, next to the place
        that already exists, and the create route accepts it.
        """
        w = self._office_plus_newcomer(app)
        fresh = app.test_client()
        owner_4 = _user(first_name="Jasur", last_name="Jurayev")
        a4 = _address(owner_4, title="reception")

        suggestions = CustomerLinkService().get_place_group_suggestions()
        assert len(suggestions) == 1
        assert suggestions[0]["address_ids"] == sorted([w["a3_id"], a4.id])
        assert suggestions[0]["distinct_customer_count"] == 2
        assert w["a1_id"] not in suggestions[0]["address_ids"]
        assert w["a2_id"] not in suggestions[0]["address_ids"]

        created = fresh.post(f"{API}/place-groups",
                             json={"addressIds": [w["a3_id"], a4.id],
                                   "reason": "co-located", "label": "Ground-floor reception"},
                             headers=w["headers"])
        assert created.status_code == 201, created.get_json()
        _db.session.expire_all()
        second_group_id = created.get_json()["data"]["place_group_id"]
        assert second_group_id != w["group_id"]

        groups = AddressGroup.query.all()
        assert len(groups) == 2
        assert _place_balance(w["a1_id"]) == Decimal("7.00")
        assert _place_balance(w["a3_id"]) == Decimal("0.00")
        # Two place groups, identical label, identical coordinates, identical
        # address text — and nothing anywhere flags it.
        assert {g.label for g in groups} == {"Ground-floor reception"}

    def test_the_manual_join_is_the_only_repair_and_it_collapses_the_two_pools_into_one(
        self, app, db
    ):
        """The repair path exists and works — `search_addresses` plus
        `POST /place-groups/<id>/addresses`. Nothing ever surfaces the need for
        it, which is the whole defect.
        """
        w = self._office_plus_newcomer(app)
        fresh = app.test_client()
        _deliver(w["owner_3"], UserAddress.query.get(w["a3_id"]), 5, actor=w["admin"])
        _db.session.expire_all()
        before = _conservation_probe()

        joined = fresh.post(f"{API}/place-groups/{w['group_id']}/addresses",
                            json={"addressIds": [w["a3_id"]], "reason": "same reception desk"},
                            headers=w["headers"])
        assert joined.status_code == 200, joined.get_json()
        _db.session.expire_all()
        assert sorted(joined.get_json()["data"]["address_ids"]) == sorted(
            [w["a1_id"], w["a2_id"], w["a3_id"]]
        )

        # The join moves the balance without a coupled ledger row of its own
        # (§7.2 re-stamps, it does not append), so conservation holds with an
        # empty append list.
        assert _assert_conserved(before) == []
        assert _place_balance(w["a1_id"]) == Decimal("12.00")
        assert _place_balance(w["a3_id"]) == Decimal("12.00")
        assert BottleBalance.query.filter_by(address_id=w["a3_id"]).first() is None
        rows = _balances(fresh, w["headers"])["items"]
        assert [r["address_group_id"] for r in rows] == [w["group_id"]]
        assert rows[0]["balance"] == 12.0
        assert rows[0]["representative_address_id"] == min(w["a1_id"], w["a2_id"], w["a3_id"])


# =========================================================================== #
# 16. WHAT ONLY REAL POSTGRES CAN SEE
# =========================================================================== #


class TestOnRealPostgres:
    """SQLite silently clamps a negative OFFSET to 0 and has FOREIGN KEYS OFF,
    so the entire SQLite half of this file is structurally blind to both of the
    defects below. `with_for_update()` is also a no-op there.
    """

    @staticmethod
    def _pg_world(pg_db):
        from flask_jwt_extended import create_access_token

        admin = User(
            email="pg-admin@example.com", phone="+998911110001",
            password_hash=hash_password("AdminPassword123!"), first_name="Pg", last_name="Admin",
            user_type=UserType.STAFF, role=UserRole.ADMIN, status=UserStatus.ACTIVE,
            is_verified=True, created_at=datetime.now(UTC),
        )
        owner = User(
            email="pg-owner@example.com", phone="+998911110002",
            password_hash=hash_password("TestPassword123!"), first_name="Pg", last_name="Owner",
            user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, status=UserStatus.ACTIVE,
            is_verified=True, created_at=datetime.now(UTC),
        )
        pg_db.session.add_all([admin, owner])
        pg_db.session.commit()
        addr = UserAddress(user_id=owner.id, title="work", full_address="1 Office St, Tashkent",
                           latitude=LAT, longitude=LNG)
        pg_db.session.add(addr)
        pg_db.session.commit()
        # `ck_orders_address_required_after_pending` is a migration-only CHECK,
        # so it exists here and NOT on the SQLite backend.
        order = Order(user_id=owner.id, status=OrderStatus.DELIVERED,
                      total_amount=Decimal("50000.00"), delivery_address_id=addr.id)
        pg_db.session.add(order)
        pg_db.session.commit()
        BottleTrackingService().record_bottles_delivered(
            order_id=order.id, user_id=owner.id, address_id=addr.id,
            quantity=Decimal("6"), actor_user_id=admin.id,
        )
        pg_db.session.commit()
        token = create_access_token(identity=str(admin.id))
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        return admin, owner, addr, headers

    @pytest.mark.parametrize(
        "query", ["?page=0", "?page=-1", "?per_page=-5", "?page=0&per_page=10"]
    )
    def test_BUG_out_of_range_pagination_500s_on_real_postgres(self, pg_app, pg_db, query):
        """FIXED — the `pytest.xfail()` branch is gone.

        WAS: `page` and `per_page` went straight from `request.args` into
        `.offset((page-1)*per_page).limit(per_page)` on EVERY paginated admin
        bottle route with no clamp. `page=0` produced OFFSET -20 -> 'ERROR:
        OFFSET must not be negative' -> 500; `per_page=-5` produced 'LIMIT must
        not be negative'. A stale or edited pagination state in the admin UI is
        a realistic way to send page=0.

        NOW all seven routes take their pagination from the single
        `_pagination_args()` clamp, so an out-of-range page is a navigation
        mistake and not a 500.

        Still asserted as an EXACT inventory: a partial fix (one route clamped,
        six not) fails this test rather than passing on a majority.
        """
        _admin, owner, addr, headers = self._pg_world(pg_db)
        client = pg_app.test_client()
        routes = [
            f"{API}/bottles/balances",
            f"{API}/bottles/ledger",
            f"{API}/bottles/ledger/{addr.id}",
            f"{API}/bottles/ledger/cluster/{owner.id}",
            f"{API}/bottles/fines",
            f"{API}/bottles/sessions",
            f"{API}/bottles/transfers",
        ]
        statuses = {}
        for url in routes:
            resp = client.get(f"{url}{query}", headers=headers)
            statuses[url] = resp.status_code
            pg_db.session.rollback()

        assert all(s == 200 for s in statuses.values()), statuses

    def test_an_absurd_per_page_is_clamped_to_the_projects_documented_cap(
        self, pg_app, pg_db
    ):
        """WAS the pin of the unclamped read; now the proof it is bounded.

        No route in `admin_bottles.py` clamped `per_page`, while the project's
        stated pagination cap is 100 (`MAX_PAGE_SIZE`). One crafted request
        pulled the entire `bottle_ledger` table into memory and JSON — and the
        merge preview's own MERGE_PREVIEW_MAX_ENTRIES=500 cap shows the team
        already treats unbounded ledger reads as dangerous.

        `per_page` now comes back as the CAP, not as what was asked for, so the
        response tells the client what it actually got.
        """
        documented_cap = 100        # the project's stated pagination cap

        _admin, _owner, addr, headers = self._pg_world(pg_db)
        client = pg_app.test_client()

        resp = client.get(f"{API}/bottles/ledger?per_page=100000", headers=headers)
        assert resp.status_code == 200, resp.get_json()
        data = resp.get_json()["data"]
        assert data["per_page"] == documented_cap, (
            "per_page must be clamped to the documented cap and REPORTED as the "
            "clamped value, not echoed back verbatim"
        )
        # The small fixture still fits in one page, so the page arithmetic is
        # unchanged for every honest request.
        assert data["pages"] == 1
        assert len(data["items"]) == data["total"] >= 1
        assert {i["address_id"] for i in data["items"]} == {addr.id}

    def test_a_nonexistent_explicit_user_id_never_reaches_the_NOT_NULL_FK(self, pg_app, pg_db):
        """FIXED — the `pytest.xfail()` branch is gone.

        WAS: with no `_assert_user_in_scope` on the adjustment /
        initial-balance routes, an explicit `user_id` that does not exist went
        straight into `bottle_ledger.user_id`, a NOT NULL FOREIGN KEY. On
        Postgres that was an IntegrityError surfacing as a 500; on the FK-off
        SQLite suite it committed a DANGLING FK that no test could see — which
        is exactly why this half is asserted HERE, on the real backend, and not
        in the SQLite classes above.

        NOW `_authorised_place_attribution` fences it as a 400 long before the
        INSERT: a nonexistent user owns no address anywhere, so it can never be
        a member of the place. The error code is asserted so a 400 arriving for
        some unrelated reason cannot pass for the fix.
        """
        _admin, _owner, addr, headers = self._pg_world(pg_db)
        client = pg_app.test_client()
        ledger_before = BottleLedger.query.count()

        adjust = client.post(f"{API}/bottles/adjustment",
                             json={"user_id": 999999, "address_id": addr.id,
                                   "adjustment": 1, "notes": "x"},
                             headers=headers)
        pg_db.session.rollback()
        virgin = UserAddress(user_id=_owner.id, title="virgin",
                             full_address="2 Office St, Tashkent", latitude=LAT, longitude=LNG)
        pg_db.session.add(virgin)
        pg_db.session.commit()
        initial = client.post(f"{API}/bottles/initial-balance",
                              json={"user_id": 999999, "address_id": virgin.id, "quantity": 4},
                              headers=headers)
        pg_db.session.rollback()

        assert BottleLedger.query.count() == ledger_before, "no row may be committed"
        assert adjust.status_code == 400, adjust.get_json()
        assert adjust.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
        assert initial.status_code == 400, initial.get_json()
        assert initial.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"

    def test_an_out_of_scope_user_id_is_refused_on_real_postgres_too(self, pg_app, pg_db):
        """The scope half of the same fix, proven where the FKs are REAL.

        WAS: the SQLite demonstration of "an out-of-scope `user_id` is
        accepted" was open to the objection that the suite runs with FOREIGN
        KEYS OFF, so it might be an artifact. It was not: with a user who
        EXISTS but owns no address at the place, every FK is satisfied and the
        row COMMITTED on Postgres exactly as it did on SQLite. The stranger's
        attribution was a production fact.

        NOW the membership fence refuses it on both backends, and this test
        keeps proving it on the one where nothing is excused by a disabled
        constraint. Contrast
        `test_a_nonexistent_explicit_user_id_never_reaches_the_NOT_NULL_FK`,
        which covers the id that has no `users` row at all.
        """
        _admin, _owner, addr, headers = self._pg_world(pg_db)
        stranger = User(
            email="pg-stranger@example.com", phone="+998911110009",
            password_hash=hash_password("TestPassword123!"), first_name="Pg", last_name="Stranger",
            user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, status=UserStatus.ACTIVE,
            is_verified=True, created_at=datetime.now(UTC),
        )
        pg_db.session.add(stranger)
        pg_db.session.commit()
        assert UserAddress.query.filter_by(user_id=stranger.id).count() == 0

        ledger_before = BottleLedger.query.count()
        resp = pg_app.test_client().post(
            f"{API}/bottles/adjustment",
            json={"user_id": stranger.id, "address_id": addr.id, "adjustment": 5, "notes": "x"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.get_data(as_text=True)
        assert resp.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"

        pg_db.session.rollback()
        pg_db.session.expire_all()
        # The write is gone in every direction: no committed row carries the
        # stranger's attribution, the row count is unchanged, and the place did
        # not move by the 5 the request asked for.
        assert BottleLedger.query.filter_by(user_id=stranger.id).count() == 0
        assert BottleLedger.query.count() == ledger_before
        assert BottleTrackingService.get_place_balance(addr.id) == Decimal("6.00")

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_adjustment_never_reaches_postgres_numeric(
        self, pg_app, pg_db, literal
    ):
        """FIXED — the `pytest.xfail()` is gone.

        WAS: Postgres `numeric` ACCEPTS 'NaN', so the adjustment COMMITTED and
        the stored balance became NaN PERMANENTLY. Every `Decimal` ordering
        comparison against it then RAISED `InvalidOperation` (not merely
        "returned False"), so `_validated_bottles_leaving`'s `max(0, place)`
        cap, `suggested_bottles_leaving`'s clamp and the COD cap all 500'd
        instead of protecting the place, and `reconcile_balance` could not
        repair it because the ledger sum was NaN too. SQLite could show none of
        this — its NOT NULL rejects NaN, so the same request was a 500 there —
        which is why this assertion has to live on the real backend.

        Extended from NaN to all three literals when the guard landed: the
        SQLite classes can only observe NaN's accidental rejection, so Postgres
        is the ONLY place `±Infinity` being refused rather than committed can be
        asserted at all.
        """
        _admin, _owner, addr, headers = self._pg_world(pg_db)
        client = pg_app.test_client()
        assert BottleTrackingService.get_place_balance(addr.id) == Decimal("6.00")
        body = json.dumps({"address_id": addr.id, "adjustment": 0.0, "notes": "x"}
                          ).replace("0.0", literal)

        resp = client.post(f"{API}/bottles/adjustment", data=body,
                           content_type="application/json", headers=headers)
        pg_db.session.rollback()
        assert resp.status_code == 400, resp.get_data(as_text=True)

        pg_db.session.expire_all()
        intact = BottleTrackingService.get_place_balance(addr.id)
        assert intact == Decimal("6.00"), intact
        # The very comparisons the poison used to break still evaluate — the
        # point of the guard, stated as behaviour rather than as a status code.
        assert Decimal("999") > intact
        assert max(Decimal("0.00"), intact) == Decimal("6.00")
        # ...and reconcile still means something, because the ledger sum is finite.
        repaired = client.post(f"{API}/bottles/reconcile/{addr.id}", headers=headers)
        pg_db.session.expire_all()
        assert repaired.status_code == 200, repaired.get_data(as_text=True)
        assert BottleTrackingService.get_place_balance(addr.id) == Decimal("6.00")

    def test_two_concurrent_admin_adjustments_on_one_shared_place_do_not_lose_an_update(
        self, pg_app, pg_db
    ):
        """`get_or_create_balance`'s `FOR UPDATE` is the ONLY serialisation on the
        adjustment route, and it is a NO-OP on SQLite — so a lost update here
        reads as 13.00 instead of 10.00 and looks like an admin miscounting. The
        lock is now SHARED BY EVERY MEMBER of a place, so two coworkers'
        addresses contend where they previously did not: the two requests below
        deliberately go through DIFFERENT member addresses.
        """
        _admin, owner, addr, headers = self._pg_world(pg_db)
        coworker = User(
            email="pg-coworker@example.com", phone="+998911110003",
            password_hash=hash_password("TestPassword123!"), first_name="Pg", last_name="Mate",
            user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, status=UserStatus.ACTIVE,
            is_verified=True, created_at=datetime.now(UTC),
        )
        pg_db.session.add(coworker)
        pg_db.session.commit()
        mate_address = UserAddress(user_id=coworker.id, title="work",
                                   full_address="1 Office St, Tashkent", latitude=LAT, longitude=LNG)
        pg_db.session.add(mate_address)
        pg_db.session.commit()

        created = pg_app.test_client().post(
            f"{API}/place-groups",
            json={"addressIds": [addr.id, mate_address.id], "reason": "same office"},
            headers=headers,
        )
        assert created.status_code == 201, created.get_json()
        group_id = created.get_json()["data"]["place_group_id"]
        first_address_id, second_address_id = addr.id, mate_address.id
        assert BottleTrackingService.get_place_balance(first_address_id) == Decimal("6.00")

        # Release every lock the test's own session holds before the race.
        pg_db.session.commit()
        pg_db.session.remove()

        barrier = threading.Barrier(2, timeout=30)
        results = {}

        def adjust(label, address_id, delta):
            with pg_app.app_context():
                from business_app import db as other

                try:
                    # A regression that deadlocks must fail loudly, not hang.
                    other.session.execute(text("SET lock_timeout = '15000ms'"))
                    client = pg_app.test_client()
                    barrier.wait()
                    resp = client.post(
                        f"{API}/bottles/adjustment",
                        json={"address_id": address_id, "adjustment": delta, "notes": label},
                        headers=headers,
                    )
                    results[label] = (resp.status_code, resp.get_json())
                except BaseException as exc:      # noqa: BLE001 - re-asserted below
                    results[label] = ("error", exc)
                finally:
                    other.session.remove()

        threads = [
            threading.Thread(target=adjust, args=("plus-three", first_address_id, 3)),
            threading.Thread(target=adjust, args=("plus-four", second_address_id, 4)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
            assert not thread.is_alive(), "an adjustment never finished — a lock was held"

        for label, (status, payload) in results.items():
            assert status == 200, f"{label} -> {status} {payload}"

        pg_db.session.expire_all()
        assert BottleTrackingService.get_place_balance(first_address_id) == Decimal("13.00"), (
            "a lost update: 6 + 3 + 4 must be 13"
        )
        rows = BottleLedger.query.filter(
            BottleLedger.address_group_id == group_id,
            BottleLedger.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT,
        ).all()
        assert len(rows) == 2
        snapshots = sorted(Decimal(str(r.balance_after)) for r in rows)
        assert snapshots in ([Decimal("9.00"), Decimal("13.00")],
                             [Decimal("10.00"), Decimal("13.00")]), snapshots
        assert len(set(snapshots)) == 2, "two rows claimed the same running total"

    def test_a_grouped_addresss_balance_row_obeys_the_scope_CHECK_on_postgres(
        self, pg_app, pg_db
    ):
        """`ck_bottle_balance_scope` is honoured on SQLite too, but the FK from
        `bottle_ledger.address_group_id` to `address_groups.id` is not — and it
        is that FK that makes deleting a memberless group impossible. Pinned
        here on the real backend so a "cleanup" that drops the row explodes in a
        test rather than in production.
        """
        from business_app.models.customer_link import AddressGroup as PgAddressGroup

        admin, owner, addr, headers = self._pg_world(pg_db)
        second = UserAddress(user_id=owner.id, title="office-2",
                             full_address="1 Office St, Tashkent", latitude=LAT, longitude=LNG)
        pg_db.session.add(second)
        pg_db.session.commit()

        created = pg_app.test_client().post(
            f"{API}/place-groups",
            json={"addressIds": [addr.id, second.id], "reason": "same office"},
            headers=headers,
        )
        assert created.status_code == 201, created.get_json()
        group_id = created.get_json()["data"]["place_group_id"]

        row = BottleBalance.query.filter_by(address_group_id=group_id).one()
        assert row.address_id is None, "ck_bottle_balance_scope: exactly one scope key"

        removed = pg_app.test_client().delete(
            f"{API}/place-groups/{group_id}/addresses/{second.id}",
            json={"reason": "moved out"}, headers=headers,
        )
        assert removed.status_code == 200, removed.get_json()
        assert removed.get_json()["data"]["dissolved"] is True
        assert BottleBalance.query.filter_by(address_group_id=group_id).first() is None
        assert PgAddressGroup.query.get(group_id) is not None, (
            "bottle_ledger.address_group_id is an FK — the memberless group row is HELD"
        )
        assert admin.id is not None
