"""Spec §7.4 — the place MERGE REVIEW, end to end over the real admin surface.

Axis: `GET /admin/place-groups/merge-preview` plus both mutating join routes
(`POST /admin/place-groups`, `POST /admin/place-groups/{id}/addresses`).

THE DISTINCTION EVERYTHING HERE RESTS ON. A place carries TWO figures that
legitimately disagree on production data:

  * the STORED balance (`bottle_balances.balance`) — what `get_place_balance`
    and every operational reader returns;
  * the LEDGER SUM (`SUM(bottle_ledger.quantity)` over the scope).

They diverge because addresses were adjusted by hand before the ledger existed
(dev address 24: stored 20.00, ZERO ledger rows). `_create_ledger_entry` moves
BOTH by the same quantity, so that gap is INVARIANT under any balance-coupled
append — which is the entire reason `_create_ledger_backfill_entry` exists as
the one balance-DECOUPLED writer.

The review converges them: backfill `stored − ledger_sum` (decoupled), then one
reversal per exclusion (coupled), then `stated − (stored − excluded_total)`
(coupled). THE GUARD, asserted here across a full cross product:

    after ANY reviewed merge,  get_place_balance(member) == ledger_sum(place)

CONSERVATION is asserted as a PAIR, never one-sided, and always over EVERY
`BottleBalance` / `BottleLedger` row in the database — a one-sided assertion
passes for a bug that lands the reviewed place correctly while zeroing another
scope:

    Σ balances after − before == Σ COUPLED quantities  (merge_exclude/correction)
    Σ ledger   after − before == that + Σ merge_backfill quantities

Everything drives real service write paths (`admin_adjust_balance`,
`record_bottles_delivered`, `record_bottles_returned`, `issue_fine`) and real
HTTP routes with real JWTs. No hand-built `BottleBalance` row appears anywhere
except where the row itself is the subject.

EIGHT PRODUCTION DEFECTS are demonstrated below. Six are `xfail(strict=True)`
tests named `test_BUG_*` (see the module-level list above the block of them);
BUG 6 is pinned by its exact 500 on real Postgres; BUG 8 — two concurrent joins
of the SAME address both commit — is a strict xfail in section 12. Every
`test_BUG_*` whose failure is "the call was accepted" is accompanied by a
PASSING test pinning the harm that acceptance causes, so the xfail cannot start
failing for a different reason (a shifted id space, a changed status code)
without something going red.
"""

import itertools
import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleLedger
from business_app.models.customer_link import AddressGroup, CustomerLinkEvent
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.serializers.bottle_serializers import (
    serialize_bottle_ledger_entry,
    serialize_customer_place_ledger_entry,
)
from business_app.services.bottle_scope import BottleScope
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.utils.exceptions import ValidationError
from business_app.utils.password_security import hash_password
from shared.enums import (
    BottleLedgerEventType,
    OrderStatus,
    UserRole,
    UserStatus,
    UserType,
)

pytestmark = pytest.mark.integration

LAT, LNG = 41.3111, 69.2797
PREVIEW = "/api/v1/admin/place-groups/merge-preview"
CREATE = "/api/v1/admin/place-groups"

_SEQ = itertools.count(1)


# --------------------------------------------------------------------------- #
# Fixtures — real rows through real paths.
# --------------------------------------------------------------------------- #

def _customer(**overrides):
    n = next(_SEQ)
    user = User(
        email=f"merge{n}@example.com",
        phone=f"+9989900{n:05d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=f"Cust{n}",
        last_name="Merge",
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
        **overrides,
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def _staff(role):
    n = next(_SEQ)
    user = User(
        email=f"staff{n}@example.com",
        phone=f"+9989901{n:05d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=f"Staff{n}",
        last_name=role.value,
        user_type=UserType.STAFF,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    _db.session.add(user)
    _db.session.commit()
    return user


def _headers_for(app, user):
    return {
        "Authorization": f"Bearer {create_access_token(identity=str(user.id))}",
        "Content-Type": "application/json",
    }


def _address(user, title="Office"):
    addr = UserAddress(
        user_id=user.id, title=title, full_address=f"{title} {next(_SEQ)}",
        latitude=LAT, longitude=LNG,
    )
    _db.session.add(addr)
    _db.session.commit()
    return addr


def _seed(user, addr, qty, notes="seed"):
    """Put `qty` bottles at the address's PLACE through the real write path."""
    entry = BottleTrackingService().admin_adjust_balance(
        user_id=user.id, address_id=addr.id, adjustment=Decimal(str(qty)),
        actor_user_id=user.id, notes=notes,
    )
    _db.session.commit()
    return entry


def _give_back(user, addr, qty):
    """A real return through the real write path (negative quantity)."""
    entry = BottleTrackingService().record_bottles_returned(
        user_id=user.id, address_id=addr.id, quantity=Decimal(str(qty)),
        actor_user_id=user.id,
    )
    _db.session.commit()
    return entry


def _deliver(user, addr, qty):
    """A real delivery through the real order write path (+quantity)."""
    n = next(_SEQ)
    order = Order(
        user_id=user.id, order_number=f"ORD-MERGE-{n}", status=OrderStatus.PENDING,
        subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"), created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.commit()
    entry = BottleTrackingService().record_bottles_delivered(
        order_id=order.id, user_id=user.id, address_id=addr.id,
        quantity=Decimal(str(qty)), actor_user_id=user.id,
    )
    _db.session.commit()
    return entry


def _wipe_ledger(addr):
    """The address-24 shape: a REAL stored balance with no ledger rows behind it.

    The balance row itself came from the real write path; only its history is
    removed, which is exactly how production got here (adjusted by hand before
    the ledger existed).
    """
    BottleLedger.query.filter_by(address_id=addr.id).delete(synchronize_session=False)
    _db.session.commit()


def _nudge_stored(addr, value):
    """Move ONLY the stored figure of a real row — the other production shape."""
    row = BottleTrackingService.get_place_balance_row(addr.id)
    assert row is not None, "nudge a row that the real write path created"
    row.balance = Decimal(str(value))
    _db.session.commit()


def _refresh():
    _db.session.expire_all()


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #

def _all_balances():
    _refresh()
    return sum((Decimal(str(b.balance or 0)) for b in BottleBalance.query.all()), Decimal("0.00"))


def _all_ledger():
    _refresh()
    return sum((Decimal(str(e.quantity or 0)) for e in BottleLedger.query.all()), Decimal("0.00"))


def _keyed_sum(*prefixes):
    _refresh()
    total = Decimal("0.00")
    for entry in BottleLedger.query.all():
        key = entry.idempotency_key or ""
        if key.startswith(prefixes):
            total += Decimal(str(entry.quantity or 0))
    return total


def _coupled():
    """Every BALANCE-COUPLED merge-review quantity — exclusions and the override.

    `merge_backfill` is a NAMED, DELIBERATE omission: it moves no balance at
    all. Restoring it here and concluding the backfill mints bottles would be
    reading the invariant backwards.
    """
    return _keyed_sum("merge_exclude:", "merge_correction:")


def _backfilled():
    return _keyed_sum("merge_backfill:")


def _place_ledger_sum(group_id):
    _refresh()
    scope = BottleScope.for_group(group_id)
    return sum(
        (Decimal(str(e.quantity or 0)) for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
        Decimal("0.00"),
    )


def _own_ledger_sum(address_id):
    _refresh()
    scope = BottleScope.for_address(address_id)
    return sum(
        (Decimal(str(e.quantity or 0)) for e in BottleLedger.query.filter(*scope.ledger_filter()).all()),
        Decimal("0.00"),
    )


def _place_balance(address_id):
    _refresh()
    return BottleTrackingService.get_place_balance(address_id)


def _place_balance_of_group(group_id):
    """Through a real member address, so the scope resolver is exercised."""
    _refresh()
    member = (
        UserAddress.query.filter_by(address_group_id=group_id)
        .order_by(UserAddress.id.asc())
        .first()
    )
    assert member is not None, f"group {group_id} has no members to read through"
    return BottleTrackingService.get_place_balance(member.id)


def _rows_of(prefix):
    _refresh()
    return [e for e in BottleLedger.query.all() if (e.idempotency_key or "").startswith(prefix)]


def _world():
    """Everything a rejected call must leave untouched."""
    _refresh()
    return {
        "groups": AddressGroup.query.count(),
        "ledger": BottleLedger.query.count(),
        "events": CustomerLinkEvent.query.count(),
        "balances": _all_balances(),
        "grouped": sorted(
            (a.id, a.address_group_id) for a in UserAddress.query.all()
        ),
    }


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _preview(client, headers, address_ids, group_id=None, exclude=None, raw_group_id=None):
    query = "address_ids=" + ",".join(str(a) for a in address_ids)
    if group_id is not None:
        query += f"&group_id={group_id}"
    if raw_group_id is not None:
        query += f"&group_id={raw_group_id}"
    if exclude is not None:
        query += f"&exclude={exclude}"
    return client.get(f"{PREVIEW}?{query}", headers=headers)


def _ok_preview(client, headers, address_ids, group_id=None, exclude=None):
    resp = _preview(client, headers, address_ids, group_id=group_id, exclude=exclude)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _error_code(resp):
    return ((resp.get_json() or {}).get("data") or {}).get("error_code")


def _error_text(resp):
    body = resp.get_json() or {}
    return " ".join(body.get("errors") or []) + " " + str(body.get("message") or "")


def _create(client, headers, address_ids, **body):
    payload = {"addressIds": address_ids}
    payload.update(body)
    return client.post(CREATE, json=payload, headers=headers)


def _add(client, headers, group_id, address_ids, **body):
    payload = {"addressIds": address_ids}
    payload.update(body)
    return client.post(f"{CREATE}/{group_id}/addresses", json=payload, headers=headers)


def _detail(client, headers, group_id):
    resp = client.get(f"{CREATE}/{group_id}", headers=headers)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


# --------------------------------------------------------------------------- #
# Scenario shapes, built from the verified dev rows.
# --------------------------------------------------------------------------- #

def _group_9_shape():
    """Dev group 9: +6 delivery at A, +5 at B, −4 return at B. Stored 7, drift 0."""
    ua, a = _customer(), None
    a = _address(ua, "A")
    ub = _customer()
    b = _address(ub, "B")
    _deliver(ua, a, 6)
    _deliver(ub, b, 5)
    _give_back(ub, b, 4)
    return (ua, a), (ub, b)


def _address_24_shape(qty="20"):
    """Dev address 24: stored 20.00 with ZERO ledger rows."""
    u = _customer()
    a = _address(u, "Home")
    _seed(u, a, qty)
    _wipe_ledger(a)
    assert _place_balance(a.id) == Decimal(qty).quantize(Decimal("0.01"))
    assert _own_ledger_sum(a.id) == Decimal("0.00")
    return u, a


# =========================================================================== #
# 1. THE PREVIEW — figures, ordering, arg parsing, purity, the cap.
# =========================================================================== #

def test_preview_of_a_clean_place_reports_six_json_numbers_and_a_chronological_running_total(
    client, db, admin_auth_headers
):
    """Group-9 shape. Every figure is a JSON number (Flask renders a bare
    Decimal as the STRING "7.00" and the panel does arithmetic on these), and
    the rows come back in (occurred_at, id) order with a merged running total."""
    (ua, a), (ub, b) = _group_9_shape()

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    for key in ("computed_balance", "stored_balance", "drift", "excluded_total",
                "resulting_balance", "projected_place_balance"):
        assert isinstance(data[key], (int, float)), f"{key} must cross as a JSON number"
        assert not isinstance(data[key], bool), f"{key} must not be a bool"
    assert data["computed_balance"] == 7
    assert data["stored_balance"] == 7
    assert data["drift"] == 0
    assert data["excluded_total"] == 0
    assert data["resulting_balance"] == 7
    assert data["projected_place_balance"] == 7

    assert len(data["entries"]) == 3
    assert [e["preview_balance_after"] for e in data["entries"]] == [6, 11, 7]
    assert [e["quantity"] for e in data["entries"]] == [6, 5, -4]
    assert data["entry_ids"] == [e["id"] for e in data["entries"]]
    assert all(e["excluded"] is False for e in data["entries"])


def test_preview_orders_same_timestamp_rows_by_id_not_by_id_alone(client, db, admin_auth_headers):
    """The order is (occurred_at, id). A BACKDATED entry must sort FIRST even
    though its id is highest — an id-only ORDER BY would render the timeline
    the admin decides against in the wrong order."""
    (ua, a), (ub, b) = _group_9_shape()
    backdated = _seed(ua, a, 2)
    backdated.occurred_at = datetime.now(UTC) - timedelta(days=30)
    _db.session.commit()

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert data["entry_ids"][0] == backdated.id
    assert data["entry_ids"] != sorted(data["entry_ids"])
    assert [e["preview_balance_after"] for e in data["entries"]] == [2, 8, 13, 9]


def test_preview_of_the_address_24_shape_reports_a_positive_drift_and_no_entries(
    client, db, admin_auth_headers
):
    """stored 20 / ledger 0 — the production shape the feature exists for. An
    EMPTY entry list must not 500 the preview or its correction anchor."""
    u, a = _address_24_shape()
    b = _address(_customer(), "Fresh")

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert data["computed_balance"] == 0
    assert data["stored_balance"] == 20
    assert data["drift"] == 20
    assert data["excluded_total"] == 0
    assert data["resulting_balance"] == 0
    assert data["projected_place_balance"] == 20
    assert data["entries"] == []
    assert data["entry_ids"] == []


def test_preview_reports_a_NEGATIVE_drift_when_the_ledger_recorded_more_than_the_place_holds(
    client, db, admin_auth_headers
):
    """The backfill is SIGNED and both directions are real. A `max(0, drift)`
    or an abs() anywhere silently drops this half and every negative-drift
    place then never converges."""
    u = _customer()
    a = _address(u, "A")
    _seed(u, a, 10)
    _nudge_stored(a, "4")
    b = _address(_customer(), "B")

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert data["computed_balance"] == 10
    assert data["stored_balance"] == 4
    assert data["drift"] == -6
    assert data["projected_place_balance"] == 4


def test_preview_of_an_existing_group_unions_the_group_scope_with_the_joiners_own_scope(
    client, db, admin_auth_headers
):
    """The candidate set is an OR of two clauses. Dropping the group clause
    turns an add-to-group preview into a create preview, and the admin decides
    against the wrong ledger."""
    (ua, a), (ub, b) = _group_9_shape()
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="one office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    uc = _customer()
    c = _address(uc, "C")
    _seed(uc, c, 5)

    data = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    assert len(data["entries"]) == 4
    assert data["computed_balance"] == 12
    assert data["stored_balance"] == 12
    assert data["drift"] == 0
    assert data["projected_place_balance"] == 12


def test_preview_of_a_rejoin_cannot_pull_a_former_groups_rows(client, db, admin_auth_headers):
    """The §7.2 selector is `address_id IN (...) AND address_group_id IS NULL`.
    Degrading it to address_id alone double-counts a place's history on every
    re-join, and the whole review arithmetic is then built on a doubled base."""
    uc, ud, ue = _customer(), _customer(), _customer()
    c, d, e = _address(uc, "C"), _address(ud, "D"), _address(ue, "E")
    _seed(uc, c, 6)
    _seed(ud, d, 3)
    _seed(ue, e, 1)
    created = _create(client, admin_auth_headers, [c.id, d.id, e.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    removed = client.delete(f"{CREATE}/{group_id}/addresses/{c.id}",
                            json={"reason": "moved out", "bottlesLeaving": 0},
                            headers=admin_auth_headers)
    assert removed.status_code == 200, removed.get_json()

    uf = _customer()
    f = _address(uf, "F")
    _seed(uf, f, 2)

    data = _ok_preview(client, admin_auth_headers, [c.id, f.id])
    assert data["entry_ids"] == [e["id"] for e in data["entries"]]
    assert all(row["address_id"] != c.id for row in data["entries"]), (
        "C's rows still carry its FORMER group and must not re-enter a new merge"
    )
    assert data["computed_balance"] == 2
    assert data["stored_balance"] == 2


def test_preview_404s_on_a_missing_address_and_on_a_missing_group(client, db, admin_auth_headers):
    """Spec §13's last line: a lookup miss is a 404, never the 500 every other
    admin route's bare `except Exception` would produce."""
    a = _address(_customer(), "A")
    b = _address(_customer(), "B")

    missing_addr = _preview(client, admin_auth_headers, [a.id, 999999])
    assert missing_addr.status_code == 404, missing_addr.get_json()
    assert "PlaceGroupMergePreview" in _error_text(missing_addr)

    missing_group = _preview(client, admin_auth_headers, [a.id, b.id], group_id=999999)
    assert missing_group.status_code == 404, missing_group.get_json()
    assert "PlaceGroupMergePreview" in _error_text(missing_group)


@pytest.mark.parametrize("group_id", [0, -1])
def test_preview_treats_group_id_zero_and_negative_as_a_lookup_miss_not_as_absent(
    client, db, admin_auth_headers, group_id
):
    """`if group_id is not None` — a truthiness check would make group 0 vanish
    into a CREATE preview labelled as an add."""
    a = _address(_customer(), "A")
    b = _address(_customer(), "B")
    resp = _preview(client, admin_auth_headers, [a.id, b.id], group_id=group_id)
    assert resp.status_code == 404, resp.get_json()


@pytest.mark.parametrize("query", ["", "address_ids=", "address_ids=,,", "address_ids=1,oops"])
def test_preview_400s_on_missing_and_malformed_address_ids(client, db, admin_auth_headers, query):
    """A silent [] would produce a confident preview of NOTHING, and the
    override would then be measured against it."""
    resp = client.get(f"{PREVIEW}?{query}", headers=admin_auth_headers)
    assert resp.status_code == 400, resp.get_json()
    assert "entries" not in (resp.get_json().get("data") or {})


def test_preview_exclude_arg_parsing_blank_empty_member_stray_and_garbage(
    client, db, admin_auth_headers
):
    """The read route is STRICT about exclusions (the committer runs its own
    copy of the fence in §7.4's order). If the decision aid ever loses
    strictness it accepts input the commit refuses."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    e2 = _seed(ub, b, 3)

    blank = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude="")
    assert blank["excluded_total"] == 0
    assert [row["excluded"] for row in blank["entries"]] == [False, False]

    hole = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=f"{e1.id},,{e2.id}")
    assert hole["excluded_total"] == 7
    assert [row["excluded"] for row in hole["entries"]] == [True, True]

    stray = _preview(client, admin_auth_headers, [a.id, b.id], exclude="999999")
    assert stray.status_code == 400, stray.get_json()
    assert _error_code(stray) == "MERGE_EXCLUSION_NOT_ELIGIBLE"

    garbage = _preview(client, admin_auth_headers, [a.id, b.id], exclude="abc")
    assert garbage.status_code == 400, garbage.get_json()
    assert _error_code(garbage) is None, "no §13 code is defined for a malformed arg"


def test_preview_exclusion_subtracts_from_resulting_but_never_from_computed(
    client, db, admin_auth_headers
):
    """`excluded` is computed per row against a hoisted id set. Computed against
    the wrong id space (index vs id) the checkboxes mark rows the admin never
    chose."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=e1.id)
    assert data["computed_balance"] == 7
    assert data["excluded_total"] == 4
    assert data["resulting_balance"] == 3
    assert data["projected_place_balance"] == 3
    assert [row["excluded"] for row in data["entries"]] == [True, False]
    assert data["entries"][0]["id"] == e1.id


def test_preview_is_a_pure_read_and_mutates_no_balance_after_column(client, db, admin_auth_headers):
    """`entry.preview_balance_after = running` writes onto LIVE ORM objects. If
    that name ever collides with a mapped column, previewing a merge the admin
    CANCELS rewrites the history of two places that never joined."""
    (ua, a), (ub, b) = _group_9_shape()
    _refresh()
    before_after = {e.id: Decimal(str(e.balance_after)) for e in BottleLedger.query.all()}
    before_balances = {row.id: Decimal(str(row.balance)) for row in BottleBalance.query.all()}
    before_count = BottleLedger.query.count()

    _ok_preview(client, admin_auth_headers, [a.id, b.id])
    _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=list(before_after)[0])

    _refresh()
    assert {e.id: Decimal(str(e.balance_after)) for e in BottleLedger.query.all()} == before_after
    assert {r.id: Decimal(str(r.balance)) for r in BottleBalance.query.all()} == before_balances
    assert BottleLedger.query.count() == before_count
    assert not hasattr(BottleLedger, "preview_balance_after") or \
        "preview_balance_after" not in BottleLedger.__table__.columns


def test_the_preview_row_shape_matches_the_real_serializer_plus_two_review_keys(
    client, db, admin_auth_headers
):
    """Derived from `serialize_bottle_ledger_entry` on a REAL row rather than a
    hand-copied key set, which goes stale silently."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _deliver(ua, a, 6)
    _give_back(ua, a, 2)
    _seed(ub, b, 3, notes="counted by hand")

    data = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    _refresh()
    # `created_at` / `occurred_at` render with the session's tzinfo, which
    # differs between the request's session and this one for the same instant —
    # compared as instants below rather than as strings.
    stamps = ("created_at", "occurred_at")
    for row in data["entries"]:
        entry = BottleLedger.query.get(row["id"])
        expected = serialize_bottle_ledger_entry(entry)
        expected["preview_balance_after"] = row["preview_balance_after"]
        expected["excluded"] = row["excluded"]
        assert set(row) == set(expected), "the panel's column set is derived from the serializer"
        assert {k: v for k, v in row.items() if k not in stamps} == \
               {k: v for k, v in expected.items() if k not in stamps}
        for stamp in stamps:
            assert isinstance(row[stamp], str) and row[stamp]
        assert isinstance(row["quantity"], (int, float)) and not isinstance(row["quantity"], bool)
        assert isinstance(row["balance_after"], (int, float))
        assert isinstance(row["preview_balance_after"], (int, float))
        assert isinstance(row["excluded"], bool)
    # The relation-derived columns the panel renders must actually be populated.
    assert data["entries"][0]["user_name"]
    assert data["entries"][0]["address_title"] == "A"


def test_the_preview_cap_admits_exactly_500_entries_and_rejects_501(client, db, admin_auth_headers):
    """`>` not `>=`: an off-by-one locks admins out of a merge they may review,
    and paging instead of rejecting would let a merge be decided against a
    PARTIAL ledger — the exact mistake the review exists to prevent."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    service = BottleTrackingService()
    for _ in range(500):
        service.admin_adjust_balance(user_id=ua.id, address_id=a.id,
                                     adjustment=Decimal("1"), actor_user_id=ua.id, notes="bulk")
    _db.session.commit()

    at_cap = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert len(at_cap["entries"]) == 500
    assert at_cap["computed_balance"] == 500

    _seed(ub, b, 1)
    over = _preview(client, admin_auth_headers, [a.id, b.id])
    assert over.status_code == 400, over.get_json()
    text = _error_text(over)
    assert "501" in text and "500" in text
    assert "entries" not in (over.get_json().get("data") or {})


def test_the_service_cap_rejects_an_override_for_a_merge_that_was_never_rendered(
    client, db, admin_auth_headers
):
    """The route's cap only stops the PREVIEW being fetched. Without the
    service-side copy a scripted client posts an override for a merge nobody
    could review — and the guard must run BEFORE any flush, so no orphan
    AddressGroup survives the rejection."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    service = BottleTrackingService()
    for _ in range(501):
        service.admin_adjust_balance(user_id=ua.id, address_id=a.id,
                                     adjustment=Decimal("1"), actor_user_id=ua.id, notes="bulk")
    _db.session.commit()
    before = _world()

    rejected = _create(client, admin_auth_headers, [a.id, b.id],
                       reason="counted 3", resultingBalance=3)
    assert rejected.status_code == 400, rejected.get_json()
    assert "501" in _error_text(rejected)
    assert _world() == before

    # ...and a PLAIN join of the very same addresses is untouched by the cap.
    plain = _create(client, admin_auth_headers, [a.id, b.id], reason="just group them")
    assert plain.status_code == 201, plain.get_json()
    group_id = plain.get_json()["data"]["place_group_id"]
    assert _rows_of("merge_backfill:") == []
    assert _rows_of("merge_correction:") == []
    assert _place_balance(a.id) == Decimal("501.00") == _place_ledger_sum(group_id)


# =========================================================================== #
# 2. EXCLUSIONS — the reversal, the bases, the fences.
# =========================================================================== #

def test_exclusions_alone_land_a_DRIFTED_place_on_stored_minus_excluded(
    client, db, admin_auth_headers
):
    """The override basis is `stored_before − excluded_total`, NOT
    `computed_balance − excluded_total`. Regressing to the ledger basis lands
    this place on −5 instead of 20 and 25 real bottles vanish."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    e_b = _seed(ub, b, 5)

    # `projected_place_balance` is `stored_balance - excluded_total`, so it only
    # reflects an exclusion the PREVIEW was told about. Both spellings pinned:
    # 25 with nothing excluded, 20 once B's +5 is dropped.
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert figures["stored_balance"] == 25
    assert figures["computed_balance"] == 5
    assert figures["projected_place_balance"] == 25
    reviewed = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=e_b.id)
    assert reviewed["projected_place_balance"] == 20    # 25 stored − 5 excluded
    assert reviewed["resulting_balance"] == 0, (
        "the LEDGER basis — this is the figure a regression would commit against"
    )

    balances_before, ledger_before = _all_balances(), _all_ledger()
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted crates",
                      excludedLedgerEntryIds=[e_b.id], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("20.00")
    assert _place_ledger_sum(group_id) == Decimal("20.00")
    assert [e.quantity for e in _rows_of("merge_backfill:")] == [Decimal("20.00")]
    assert [e.quantity for e in _rows_of("merge_exclude:")] == [Decimal("-5.00")]
    assert _rows_of("merge_correction:") == []
    # Conservation, as the PAIR.
    assert _all_balances() - balances_before == _coupled()
    assert _all_ledger() - ledger_before == _coupled() + _backfilled()


def test_exclusions_alone_on_a_CLEAN_place_write_no_backfill_row_at_all(
    client, db, admin_auth_headers
):
    """`if backfill != 0` is the only thing stopping a 0-quantity DECOUPLED row
    on every clean merge — which would pollute every audit trail and make the
    decoupled-writer fence meaningless."""
    (ua, a), (ub, b) = _group_9_shape()
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    the_return = next(row for row in figures["entries"] if row["quantity"] == -4)

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="that return never happened",
                      excludedLedgerEntryIds=[the_return["id"]],
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _rows_of("merge_backfill:") == []
    assert _place_balance(a.id) == Decimal("11.00")
    assert _place_ledger_sum(group_id) == Decimal("11.00")


def test_an_exclusion_appends_a_reversal_and_never_rewrites_the_original(
    client, db, admin_auth_headers
):
    """A cheaper implementation would UPDATE the quantity to 0 or DELETE the
    row, destroying the audit trail and the conservation pair itself."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    original_id, original_qty = e1.id, Decimal(str(e1.quantity))

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="posted to the wrong place",
                      excludedLedgerEntryIds=[original_id], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    _refresh()
    survivor = BottleLedger.query.get(original_id)
    assert survivor is not None and Decimal(str(survivor.quantity)) == original_qty

    reversal = BottleLedger.query.filter_by(
        idempotency_key=f"merge_exclude:{group_id}:{_last_event_id()}:{original_id}"
    ).one()
    assert reversal.quantity == -original_qty
    assert reversal.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    assert reversal.entry_metadata["source"] == "merge_exclude"
    assert reversal.entry_metadata["excluded_ledger_entry_id"] == original_id
    assert reversal.entry_metadata["reason"] == "posted to the wrong place"
    assert reversal.entry_metadata["acting_admin_id"] is not None
    # The reversal is attributed to the very entry it neutralises.
    assert (reversal.user_id, reversal.address_id) == (survivor.user_id, survivor.address_id)
    # ...and it is BALANCE-COUPLED, so BOTH figures moved by exactly -4.
    assert _place_balance(a.id) == Decimal("3.00") == _place_ledger_sum(group_id)
    assert reversal.address_group_id == group_id
    assert _rows_of("merge_backfill:") == []


def _last_event_id():
    _refresh()
    return CustomerLinkEvent.query.order_by(CustomerLinkEvent.id.desc()).first().id


@pytest.mark.parametrize("which", ["first", "last", "both"])
def test_excluding_the_boundary_entries_of_the_merged_timeline(client, db, admin_auth_headers, which):
    """Boundary handling in the running-total loop and in `sorted(excluded)`."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    _seed(ua, a, 2)
    _seed(ub, b, -1)
    _seed(ub, b, 5)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert figures["computed_balance"] == 13
    ids = figures["entry_ids"]
    by_id = {row["id"]: Decimal(str(row["quantity"])) for row in figures["entries"]}
    chosen = {"first": [ids[0]], "last": [ids[-1]], "both": [ids[0], ids[-1]]}[which]
    expected = Decimal("13") - sum((by_id[i] for i in chosen), Decimal("0"))

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="boundary",
                      excludedLedgerEntryIds=chosen, previewEntryIds=ids)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == expected
    assert _place_ledger_sum(group_id) == expected
    reversals = _rows_of("merge_exclude:")
    assert len(reversals) == len(chosen)
    assert len({r.idempotency_key for r in reversals}) == len(chosen)


def test_excluding_every_entry_in_the_merge_empties_the_place(client, db, admin_auth_headers):
    """`excluded_total == computed_balance` is the degenerate case; a
    `resulting == 0 means no review` shortcut would skip the writes and leave
    the place holding 7."""
    (ua, a), (ub, b) = _group_9_shape()
    balances_before, ledger_before = _all_balances(), _all_ledger()

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="all of it was wrong",
                      excludedLedgerEntryIds=figures["entry_ids"],
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("0.00")
    assert _place_ledger_sum(group_id) == Decimal("0.00")
    assert len(_rows_of("merge_exclude:")) == 3
    assert _rows_of("merge_correction:") == []
    assert _all_balances() - balances_before == _coupled()
    assert _all_ledger() - ledger_before == _coupled() + _backfilled()


def test_excluding_a_zero_quantity_fine_entry_appends_a_zero_reversal_and_burns_the_id(
    client, db, admin_auth_headers
):
    """PINS CURRENT BEHAVIOUR. `if delta != 0` guards the correction but nothing
    guards a 0-quantity EXCLUSION: a zero-quantity `merge_exclude` row is
    appended, no balance moves, and the entry id is now PERMANENTLY burned by
    the double-exclusion fence. Either behaviour is defensible — pinned so a
    refactor cannot flip it silently, and so the burn is visible."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 5)
    BottleTrackingService().issue_fine(
        user_id=ua.id, address_id=a.id, quantity=Decimal("2"),
        fine_amount=Decimal("50000"), actor_user_id=ua.id, notes="missing crates",
    )
    _db.session.commit()
    _seed(ub, b, 3)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    fine_row = next(r for r in figures["entries"] if r["event_type"] == "fine_issued")
    assert fine_row["quantity"] == 0

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="fine logged twice",
                      excludedLedgerEntryIds=[fine_row["id"]], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    reversal = _rows_of("merge_exclude:")
    assert len(reversal) == 1
    assert Decimal(str(reversal[0].quantity)) == Decimal("0")
    assert _place_balance(a.id) == Decimal("8.00")
    assert _place_ledger_sum(group_id) == Decimal("8.00")

    # The id is burned for ever — the entry is still in the group's scope, so a
    # later add-preview offers it again, and the fence refuses it.
    uc = _customer()
    c = _address(uc, "C")
    again = _add(client, admin_auth_headers, group_id, [c.id], reason="new hire",
                 excludedLedgerEntryIds=[fine_row["id"]])
    assert again.status_code == 400, again.get_json()
    assert _error_code(again) == "MERGE_EXCLUSION_NOT_ELIGIBLE"


def test_excluding_one_half_of_a_zero_sum_fine_pair_unbalances_it(client, db, admin_auth_headers):
    """PINS CURRENT BEHAVIOUR. Nothing in the code recognises PAIRED entries
    (FINE_ISSUED 0 / FINE_PAID −N, or the place_dissolve halves). An admin
    excluding one half is a plausible real action and the place moves by
    exactly +|q| — pinned so any future pairing rule is a deliberate change."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 5)
    service = BottleTrackingService()
    fine = service.issue_fine(user_id=ua.id, address_id=a.id, quantity=Decimal("2"),
                              fine_amount=Decimal("50000"), actor_user_id=ua.id)
    service.mark_fine_paid(fine.id, actor_user_id=ua.id)
    _db.session.commit()
    assert _place_balance(a.id) == Decimal("3.00")

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    paid = next(r for r in figures["entries"] if r["event_type"] == "fine_paid")
    assert paid["quantity"] == -2

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="fine was settled in cash",
                      excludedLedgerEntryIds=[paid["id"]], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("5.00")
    assert _place_ledger_sum(group_id) == Decimal("5.00")
    # The FINE_ISSUED half is still there, now unpaired: the pair no longer sums
    # to -2 because only one of its halves was reversed. Asserted as the exact
    # (event_type, quantity) multiset so a reversal landing on the wrong half —
    # or an implementation that quietly reversed BOTH halves — fails here.
    _refresh()
    rows = BottleLedger.query.filter(BottleLedger.address_group_id == group_id).all()
    assert sorted((e.event_type.value, str(Decimal(str(e.quantity)))) for e in rows) == sorted([
        ("admin_adjustment", "5.00"),        # A's seed (B has none)
        ("fine_issued", "0.00"),             # the surviving, now unpaired half
        ("fine_paid", "-2.00"),              # the original, NEVER rewritten
        ("admin_adjustment", "2.00"),        # the merge_exclude reversal of it
    ])


def test_an_ineligible_exclusion_is_refused_by_the_preview_and_by_the_commit(
    client, db, admin_auth_headers
):
    """The committer runs the fence ITSELF (its own preview uses
    strict_exclusions=False so §7.4's guard ORDER holds). Delete that copy and
    an admin can reverse an entry belonging to somebody else's place."""
    ua, ub, uc = _customer(), _customer(), _customer()
    a, b, c = _address(ua, "A"), _address(ub, "B"), _address(uc, "Elsewhere")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    foreign = _seed(uc, c, 9)
    before = _world()

    for bad in (999999, foreign.id):
        peek = _preview(client, admin_auth_headers, [a.id, b.id], exclude=bad)
        assert peek.status_code == 400, peek.get_json()
        assert _error_code(peek) == "MERGE_EXCLUSION_NOT_ELIGIBLE"

        commit = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                         excludedLedgerEntryIds=[bad])
        assert commit.status_code == 400, commit.get_json()
        assert _error_code(commit) == "MERGE_EXCLUSION_NOT_ELIGIBLE"

    assert _world() == before


def test_an_already_excluded_entry_cannot_be_excluded_a_second_time(client, db, admin_auth_headers):
    """The idempotency key is EPISODE-scoped, so `_create_ledger_entry`'s own
    idempotency check would NOT catch a second reversal — it would destroy the
    bottles twice. The LIKE fence is the only guard."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="wrong place",
                      excludedLedgerEntryIds=[e1.id], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    balance_after_first = _place_balance(a.id)
    assert balance_after_first == Decimal("3.00") == _place_ledger_sum(group_id)

    uc = _customer()
    c = _address(uc, "C")
    again = _add(client, admin_auth_headers, group_id, [c.id], reason="new hire",
                 excludedLedgerEntryIds=[e1.id])
    assert again.status_code == 400, again.get_json()
    assert _error_code(again) == "MERGE_EXCLUSION_NOT_ELIGIBLE"
    assert _place_balance(a.id) == balance_after_first == _place_ledger_sum(group_id)
    assert len(_rows_of("merge_exclude:")) == 1
    _refresh()
    assert UserAddress.query.get(c.id).address_group_id is None, "the refused join wrote nothing"


def test_the_double_exclusion_fence_anchors_on_the_colon_and_does_not_match_by_suffix(
    client, db, admin_auth_headers
):
    """The pattern is `merge_exclude:%:{id}`. Dropping the leading colon
    (`merge_exclude:%{id}`) makes entry 41 shadow entry 1 FOR EVER — a
    one-character regression with a permanent, silent effect."""
    # A small-id entry at its own place...
    us, ud = _customer(), _customer()
    small_addr, d = _address(us, "Small"), _address(ud, "D")
    small = _seed(us, small_addr, 7)

    # ...and, at a different place, an entry whose id ENDS WITH the small id.
    ux, uy = _customer(), _customer()
    x, y = _address(ux, "X"), _address(uy, "Y")
    big = None
    for _ in range(200):
        candidate = _seed(ux, x, 1)
        if candidate.id != small.id and str(candidate.id).endswith(str(small.id)):
            big = candidate
            break
    assert big is not None, "could not build the id-suffix shadow pair"
    _seed(uy, y, 2)

    xy = _ok_preview(client, admin_auth_headers, [x.id, y.id])
    first = _create(client, admin_auth_headers, [x.id, y.id], reason="drop the big one",
                    excludedLedgerEntryIds=[big.id], previewEntryIds=xy["entry_ids"])
    assert first.status_code == 201, first.get_json()
    xy_group = first.get_json()["data"]["place_group_id"]
    assert any(r.idempotency_key.endswith(f":{big.id}") for r in _rows_of("merge_exclude:"))
    xy_expected = Decimal(str(xy["computed_balance"] - 1)).quantize(Decimal("0.01"))
    assert _place_balance(x.id) == xy_expected == _place_ledger_sum(xy_group)

    sd = _ok_preview(client, admin_auth_headers, [small_addr.id, d.id])
    second = _create(client, admin_auth_headers, [small_addr.id, d.id],
                     reason="a genuinely different merge",
                     excludedLedgerEntryIds=[small.id], previewEntryIds=sd["entry_ids"])
    assert second.status_code == 201, second.get_json()
    assert any(r.idempotency_key.endswith(f":{small.id}") for r in _rows_of("merge_exclude:"))
    sd_group = second.get_json()["data"]["place_group_id"]
    assert _place_balance(small_addr.id) == Decimal("0.00") == _place_ledger_sum(sd_group)
    # ...and the FIRST merge is untouched by the second: a suffix-matching fence
    # would have refused the second call, but a fence matching the other way
    # round would have reversed the big entry twice.
    assert _place_balance(x.id) == xy_expected == _place_ledger_sum(xy_group)
    assert len(_rows_of("merge_exclude:")) == 2


# =========================================================================== #
# 3. THE OVERRIDE — the stated number, and the backfill that makes it stick.
# =========================================================================== #

def test_override_alone_on_the_address_24_shape_lands_BOTH_figures_on_the_stated_number(
    client, db, admin_auth_headers
):
    """Two earlier designs failed HERE: measuring the delta against the ledger
    gave 32; absorbing the drift as a coupled −20 drove the ledger to −8 while
    the balance read 12, so the panel's Reconcile button would then set the
    balance to −8 and destroy the admin's number."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    balances_before, ledger_before = _all_balances(), _all_ledger()

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert (figures["drift"], figures["projected_place_balance"]) == (20, 20)

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12 crates",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("12.00")
    assert _place_ledger_sum(group_id) == Decimal("12.00")
    assert [e.quantity for e in _rows_of("merge_backfill:")] == [Decimal("20.00")]   # POSITIVE
    assert [e.quantity for e in _rows_of("merge_correction:")] == [Decimal("-8.00")]
    assert _detail(client, admin_auth_headers, group_id)["place_balance"] == 12
    assert _all_balances() - balances_before == _coupled()
    assert _all_ledger() - ledger_before == _coupled() + _backfilled()


def test_override_equal_to_the_projected_figure_still_backfills_but_appends_no_correction(
    client, db, admin_auth_headers
):
    """`if delta != 0` must not be conflated with `_has_merge_review`. If the
    apply half short-circuits on delta==0 the backfill is skipped and the place
    stays permanently non-convergent while reporting success."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="20 is right",
                      resultingBalance=figures["projected_place_balance"],
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert [e.quantity for e in _rows_of("merge_backfill:")] == [Decimal("20.00")]
    assert _rows_of("merge_correction:") == []
    assert _place_balance(a.id) == Decimal("20.00") == _place_ledger_sum(group_id)


def test_override_equal_to_the_current_figure_on_a_CLEAN_place_writes_nothing_at_all(
    client, db, admin_auth_headers
):
    """Both guards must hold at once. A stray 0-quantity row here breaks the
    conservation split's ability to tell coupled from decoupled BY KEY."""
    (ua, a), (ub, b) = _group_9_shape()
    balances_before, ledger_before = _all_balances(), _all_ledger()
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="7 confirmed",
                      resultingBalance=7, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _rows_of("merge_backfill:") == []
    assert _rows_of("merge_correction:") == []
    assert _place_balance(a.id) == Decimal("7.00") == _place_ledger_sum(group_id)
    assert _all_balances() == balances_before
    assert _all_ledger() == ledger_before
    # The DECISION is still on the record even though nothing moved.
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.event_metadata["resulting_balance"] == "7"


def test_override_to_zero_is_a_real_override_and_not_a_falsy_no_op(client, db, admin_auth_headers):
    """`resulting_balance is not None` is the ONLY thing separating 0 from "no
    override". A truthiness check makes stating zero a silent no-op — the admin
    says the place is empty and the system keeps 25 bottles."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    _seed(ub, b, 5)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="the place is empty",
                      resultingBalance=0, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("0.00")
    assert _place_ledger_sum(group_id) == Decimal("0.00")
    assert [e.quantity for e in _rows_of("merge_backfill:")] == [Decimal("20.00")]
    assert [e.quantity for e in _rows_of("merge_correction:")] == [Decimal("-25.00")]
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.event_metadata["resulting_balance"] == "0"


def test_override_to_a_negative_number_is_accepted_and_clamps_every_departure_prefill(
    client, db, admin_auth_headers
):
    """A place can legitimately be over-returned. `_coerce_resulting_balance` is
    deliberately NOT range-checked; the clamp lives downstream, where an
    unclamped prefill would be a value the dialog's own OK button rejects."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 5)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="over-returned",
                      resultingBalance=-3, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("-3.00")
    assert _place_ledger_sum(group_id) == Decimal("-3.00")
    assert [e.quantity for e in _rows_of("merge_correction:")] == [Decimal("-8.00")]

    detail = _detail(client, admin_auth_headers, group_id)
    assert detail["place_balance"] == -3
    assert [m["suggested_bottles_leaving"] for m in detail["members"]] == [0, 0]
    for member in (a, b):
        assert BottleTrackingService.suggested_bottles_leaving(group_id, member.id) == Decimal("0.00")


def test_exclusions_plus_override_land_on_the_stated_number_on_a_clean_place(
    client, db, admin_auth_headers
):
    """If the correction is measured against the PRE-exclusion figure it
    double-counts every exclusion (an admin stating 5 gets 9); if the order
    inverts, the running-snapshot pass sees the wrong last row."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                      excludedLedgerEntryIds=[e1.id], resultingBalance=5,
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _detail(client, admin_auth_headers, group_id)["place_balance"] == 5
    assert _place_balance(a.id) == Decimal("5.00") == _place_ledger_sum(group_id)
    assert [e.quantity for e in _rows_of("merge_exclude:")] == [Decimal("-4.00")]
    assert [e.quantity for e in _rows_of("merge_correction:")] == [Decimal("2.00")]
    # ORDER: the exclusions land BEFORE the correction.
    exclusion = _rows_of("merge_exclude:")[0]
    correction = _rows_of("merge_correction:")[0]
    assert exclusion.id < correction.id


def test_exclusions_plus_override_on_a_DRIFTED_place_use_the_stored_basis(
    client, db, admin_auth_headers
):
    """The ONLY case where the two possible bases differ AND an exclusion is
    present: `computed − excluded` = 0 versus `stored − excluded` = 20.
    Regressing to the ledger basis makes an admin stating 12 get 32."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    e_b = _seed(ub, b, 5)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=e_b.id)
    assert figures["computed_balance"] == 5
    assert figures["stored_balance"] == 25
    assert figures["drift"] == 20
    assert figures["excluded_total"] == 5
    assert figures["projected_place_balance"] == 20

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      excludedLedgerEntryIds=[e_b.id], resultingBalance=12,
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert [e.quantity for e in _rows_of("merge_backfill:")] == [Decimal("20.00")]
    assert [e.quantity for e in _rows_of("merge_exclude:")] == [Decimal("-5.00")]
    assert [e.quantity for e in _rows_of("merge_correction:")] == [Decimal("-8.00")]
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)


def test_the_backfill_moves_the_ledger_and_NOT_the_balance(client, db, admin_auth_headers):
    """The single load-bearing asymmetry in the design. If
    `_create_ledger_backfill_entry` is ever "simplified" into
    `_create_ledger_entry`, the drift is minted a second time and the place
    jumps to 40."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    _refresh()
    balances_before = _all_balances()
    ledger_before = _all_ledger()
    row_values_before = sorted(Decimal(str(r.balance)) for r in BottleBalance.query.all())

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="align the ledger",
                      resultingBalance=figures["projected_place_balance"],
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _rows_of("merge_correction:") == []
    assert _all_balances() - balances_before == Decimal("0.00"), "a backfill moves NO balance"
    assert _all_ledger() - ledger_before == Decimal("20.00") == _backfilled()
    _refresh()
    assert sorted(Decimal(str(r.balance)) for r in BottleBalance.query.all()) == row_values_before
    # The point of the asymmetry: it is what makes the two figures CONVERGE.
    assert _place_balance(a.id) == Decimal("20.00") == _place_ledger_sum(group_id)


def test_the_backfill_records_everything_needed_to_reconstruct_it(client, db, admin_auth_headers):
    """`stored_before` / `ledger_sum_before` are quantized STRINGS so an audit
    row cannot read "0" on one place and "0.00" on the next, and so a float
    cannot turn "5" into "4.999999999999999". The note is SIGN-NEUTRAL: this
    entry is written for both directions."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    admin_id = User.query.filter_by(role=UserRole.ADMIN).one().id

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12 crates",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()

    backfill = _rows_of("merge_backfill:")[0]
    # `acting_admin_id`'s TYPE is asserted separately, by
    # `test_BUG_the_audit_metadata_records_the_acting_admin_as_a_JSON_STRING`
    # below: over HTTP it arrives as the JWT identity STRING. Its VALUE is
    # asserted here, so a wrong admin id still fails this test.
    assert int(backfill.entry_metadata["acting_admin_id"]) == admin_id
    assert {k: v for k, v in backfill.entry_metadata.items() if k != "acting_admin_id"} == {
        "source": "merge_backfill",
        "reason": "counted 12 crates",
        "stored_before": "20.00",
        "ledger_sum_before": "0.00",
        "stated_resulting_balance": "12",
    }
    assert backfill.event_type == BottleLedgerEventType.ADMIN_ADJUSTMENT
    assert backfill.actor_user_id == admin_id
    lowered = (backfill.notes or "").lower()
    for sign_word in ("delivered", "returned", "arrived", "left the", "collected"):
        assert sign_word not in lowered, f"a sign-assuming note is false on a negative backfill: {backfill.notes}"


def test_the_correction_records_the_full_basis_it_was_measured_against(client, db, admin_auth_headers):
    """`post_exclusion_balance` and `preview_resulting_balance` DIFFER on a
    drifted place; recording only one makes a later dispute unresolvable."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    e_b = _seed(ub, b, 5)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id], exclude=e_b.id)
    admin_id = User.query.filter_by(role=UserRole.ADMIN).one().id

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      excludedLedgerEntryIds=[e_b.id], resultingBalance=12,
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()

    correction = _rows_of("merge_correction:")[0]
    assert int(correction.entry_metadata["acting_admin_id"]) == admin_id   # type: see the BUG test
    assert {k: v for k, v in correction.entry_metadata.items() if k != "acting_admin_id"} == {
        "source": "merge_correction",
        "reason": "counted 12",
        "stored_before": "25.00",
        "ledger_sum_before": "5.00",
        "post_exclusion_balance": "20.00",
        "preview_resulting_balance": "0.00",
        "stated_resulting_balance": "12",
    }
    assert correction.entry_metadata["post_exclusion_balance"] != \
        correction.entry_metadata["preview_resulting_balance"]


def test_BUG_the_audit_metadata_records_the_acting_admin_as_a_JSON_STRING(
    client, db, admin_auth_headers
):
    """FIXED — the strict xfail is gone.

    WAS: both join routes passed `g.current_user_id` — the raw JWT identity, a
    STRING — as `acting_admin_id`, whose signature says `int`. Every other
    landing spot is an Integer column and coerced it, but `entry_metadata` is
    JSON and stored the string "1": one and the same audit row carried
    `actor_user_id=1` beside `acting_admin_id="1"`, the field's type depended on
    whether the caller was HTTP or a service, and a consumer joining metadata to
    `users.id` got nothing back.

    NOW every place/customer-link route goes through `_acting_admin_id()`, the
    same discipline the quantized figures beside it already follow.
    """
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    admin_id = User.query.filter_by(role=UserRole.ADMIN).one().id

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()

    for prefix in ("merge_backfill:", "merge_correction:"):
        row = _rows_of(prefix)[0]
        assert row.actor_user_id == admin_id                    # the Integer column: fine
        assert row.entry_metadata["acting_admin_id"] == admin_id, (
            f"{prefix} metadata carries {row.entry_metadata['acting_admin_id']!r}, "
            f"not the int {admin_id!r} its own FK column stores"
        )
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert isinstance(event.acting_admin_id, int)


@pytest.mark.parametrize(
    "key",
    [None, "", "merge_correction:1:2", "delivery:5", "MERGE_BACKFILL:1:2", "merge_backfil:1:2",
     "merge_exclude:1:2:3"],
)
def test_the_decoupled_writer_refuses_every_key_outside_its_namespace(db, key):
    """`startswith(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)` — case-sensitive and
    prefix-based. Loosening it (or adding a namespace without adding a
    conservation split) makes the invariant uncheckable.

    `merge_exclude:1:2:3` is in the list for a specific reason: it is the OBVIOUS
    fix for BUG 7 (route the reversal of a ledger-only `merge_backfill` through
    this writer so the reversal is decoupled too) and it does not work as
    written — the key fence rejects it. That fix therefore has to add a
    namespace to `BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES` and split it out of
    `_coupled()` in every conservation oracle, which is a much larger change
    than dropping `merge_backfill:` rows from the exclusion-eligible set."""
    u = _customer()
    a = _address(u, "A")
    before = BottleLedger.query.count()
    with pytest.raises(ValidationError) as exc:
        BottleTrackingService()._create_ledger_backfill_entry(
            scope=BottleScope.for_address(a.id), user_id=u.id, address_id=a.id,
            quantity=Decimal("5"), actor_user_id=u.id, idempotency_key=key,
        )
    assert exc.value.error_code == "BOTTLE_DECOUPLED_KEY_REQUIRED"
    _db.session.rollback()
    assert BottleLedger.query.count() == before


# =========================================================================== #
# 4. THE GUARD — after ANY reviewed merge, get_place_balance == ledger_sum.
# =========================================================================== #

def _shape(kind):
    """One address whose place carries the requested drift, via real paths."""
    u = _customer()
    a = _address(u, f"shape-{kind}")
    if kind == "clean":
        _seed(u, a, 6)
    elif kind == "plus20":
        _seed(u, a, 20)
        _wipe_ledger(a)
    elif kind == "minus6":
        _seed(u, a, 10)
        _nudge_stored(a, "4")
    else:                                                   # pragma: no cover
        raise AssertionError(kind)
    return u, a


def _matrix_setup(client, headers, drift, entry_point):
    """Returns (address_ids, group_id_or_None, preview, commit_callable)."""
    u_a, a = _shape(drift)
    u_b = _customer()
    b = _address(u_b, "clean-5")
    _seed(u_b, b, 5)

    if entry_point == "create":
        preview = _ok_preview(client, headers, [a.id, b.id])

        def commit(**body):
            return _create(client, headers, [a.id, b.id], **body), 201
        return a, preview, commit

    u_f = _customer()
    f = _address(u_f, "filler")
    seeded = _create(client, headers, [b.id, f.id], reason="existing place")
    assert seeded.status_code == 201, seeded.get_json()
    group_id = seeded.get_json()["data"]["place_group_id"]
    preview = _ok_preview(client, headers, [a.id], group_id=group_id)

    def commit(**body):
        return _add(client, headers, group_id, [a.id], **body), 200
    return a, preview, commit


_STATED = ["none", "zero", "five", "negative", "projected", "projected_plus_one"]
_MATRIX = [
    (drift, exclusions, stated, entry_point)
    for drift in ("clean", "plus20", "minus6")
    for exclusions in ("none", "one", "all")
    for stated in _STATED
    for entry_point in ("create", "add")
    if not (exclusions == "none" and stated == "none")
]


@pytest.mark.parametrize("drift,exclusions,stated,entry_point", _MATRIX)
def test_THE_GUARD_a_reviewed_merge_always_leaves_the_place_balance_equal_to_its_ledger_sum(
    client, db, admin_auth_headers, drift, exclusions, stated, entry_point
):
    """The strongest single guard on the feature, over the full cross product.

    It is what makes the still-exposed Reconcile button a NO-OP on a reviewed
    place instead of a destroyer of it. Any change to the backfill sign, the
    correction basis, or the order of the three appends breaks exactly one cell
    of this matrix.
    """
    a, preview, commit = _matrix_setup(client, admin_auth_headers, drift, entry_point)

    body = {"reason": "counted the crates"}
    if exclusions == "one" and preview["entry_ids"]:
        body["excludedLedgerEntryIds"] = preview["entry_ids"][:1]
    elif exclusions == "all" and preview["entry_ids"]:
        body["excludedLedgerEntryIds"] = preview["entry_ids"]
    projected = preview["projected_place_balance"]
    stated_value = {
        "none": None, "zero": 0, "five": 5, "negative": -3,
        "projected": projected, "projected_plus_one": projected + 1,
    }[stated]
    if stated_value is not None:
        body["resultingBalance"] = stated_value
    if not body.get("excludedLedgerEntryIds") and "resultingBalance" not in body:
        pytest.skip("no review requested for this cell (empty candidate set)")
    body["previewEntryIds"] = preview["entry_ids"]

    resp, expected_status = commit(**body)
    assert resp.status_code == expected_status, resp.get_json()
    group_id = resp.get_json()["data"]["place_group_id"]

    place = _place_balance(a.id)
    ledger = _place_ledger_sum(group_id)
    assert place == ledger, (
        f"drift={drift} exclusions={exclusions} stated={stated} via={entry_point}: "
        f"place balance {place} != ledger sum {ledger}"
    )
    if stated_value is not None:
        assert place == Decimal(str(stated_value)).quantize(Decimal("0.01")), (
            "the admin's stated number is what the place must hold"
        )
    assert _detail(client, admin_auth_headers, group_id)["place_balance"] == float(place)


@pytest.mark.parametrize("variant", ["plain", "exclusions", "override", "both", "drifted"])
def test_conservation_holds_across_EVERY_scope_for_every_review_variant(
    client, db, admin_auth_headers, variant
):
    """Σ balances after − before == Σ COUPLED quantities, exactly; and
    Σ ledger after − before == that plus the backfill. An UNRELATED third
    place's row must be byte-identical afterwards — a one-sided assertion
    passes for a bug that lands the reviewed place correctly while zeroing
    another scope."""
    if variant == "drifted":
        u_a, a = _address_24_shape()
    else:
        u_a = _customer()
        a = _address(u_a, "A")
        _seed(u_a, a, 6)
    u_b = _customer()
    b = _address(u_b, "B")
    _seed(u_b, b, 5)

    # A third, unrelated place that must not move.
    u_c = _customer()
    c = _address(u_c, "Unrelated")
    _seed(u_c, c, 9)
    _refresh()
    third_row = BottleTrackingService.get_place_balance_row(c.id)
    third_before = (third_row.id, Decimal(str(third_row.balance)))

    balances_before, ledger_before = _all_balances(), _all_ledger()
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    body = {"reason": "variant " + variant, "previewEntryIds": figures["entry_ids"]}
    if variant in ("exclusions", "both"):
        body["excludedLedgerEntryIds"] = figures["entry_ids"][:1]
    if variant in ("override", "both", "drifted"):
        body["resultingBalance"] = 4
    if variant == "plain":
        body = {"reason": "plain join"}

    created = _create(client, admin_auth_headers, [a.id, b.id], **body)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _all_balances() - balances_before == _coupled()
    assert _all_ledger() - ledger_before == _coupled() + _backfilled()
    _refresh()
    still = BottleBalance.query.get(third_before[0])
    assert (still.id, Decimal(str(still.balance))) == third_before
    assert _place_balance(c.id) == Decimal("9.00")
    if variant != "plain":
        assert _place_balance(a.id) == _place_ledger_sum(group_id)


def test_reconcile_after_a_reviewed_merge_is_a_no_op(client, db, admin_auth_headers):
    """`reconcile_balance` assigns balance = ledger_sum UNCONDITIONALLY and logs
    only a warning. Convergence is what neutralises it."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()

    resp = client.post(f"/api/v1/admin/bottles/reconcile/{a.id}", headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["discrepancy"] == 0
    assert data["corrected"] is False
    assert data["previous_balance"] == data["recalculated_balance"] == 12.0
    assert _place_balance(a.id) == Decimal("12.00")


def test_reconcile_after_a_PLAIN_join_of_a_drifted_place_still_destroys_the_carried_figure(
    client, db, admin_auth_headers
):
    """PINS THE DESTRUCTIVE BEHAVIOUR deliberately. This is why the review must
    be used and why Plan C never calls `reconcile_balance`. If somebody
    "helpfully" calls it from the join path, every place seeded before the
    ledger existed is zeroed — pinned so that change cannot land quietly."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="plain join")
    assert created.status_code == 201, created.get_json()
    assert _place_balance(a.id) == Decimal("20.00")

    resp = client.post(f"/api/v1/admin/bottles/reconcile/{a.id}", headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()["data"]
    assert data["discrepancy"] == 20
    assert data["corrected"] is True
    assert _place_balance(a.id) == Decimal("0.00")


# =========================================================================== #
# 5. STALENESS — the window between an unlocked read and a locked write.
# =========================================================================== #

def test_a_delivery_landing_between_the_preview_and_the_commit_makes_the_preview_stale(
    client, db, admin_auth_headers
):
    """The concrete real-world race the guard exists for. It must run BEFORE
    any flush: a rejected merge that left a flushed AddressGroup behind would
    be adopted by the next commit on the session."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    _deliver(ua, a, 2)                      # a driver hands over two crates
    before = _world()

    stale = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                    resultingBalance=5, previewEntryIds=figures["entry_ids"])
    assert stale.status_code == 400, stale.get_json()
    assert _error_code(stale) == "MERGE_PREVIEW_STALE"
    assert _world() == before
    assert _place_balance(a.id) == Decimal("6.00")


def test_a_stale_preview_is_rejected_when_an_entry_DISAPPEARS_from_the_candidate_set(
    client, db, admin_auth_headers
):
    """A two-way SET equality, not a length or a subset check. A subset check
    would accept a SHRUNKEN set and measure the override against a merge that
    no longer exists.

    The shrink must happen WITHOUT grouping either joiner: grouping one of them
    trips `_assert_place_group_eligible`, which runs BEFORE
    `_validate_merge_review` and answers PLACE_GROUP_ADDRESS_ALREADY_GROUPED —
    so a test built that way never reaches the staleness guard and pins nothing
    about it. Here B's own rows are removed from under the preview
    (`_wipe_ledger`, the same purge the address-24 shape uses) while both
    addresses stay ungrouped, so the guard really is what fires.
    """
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    _seed(ub, b, 2)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert len(figures["entry_ids"]) == 3

    _wipe_ledger(b)                                  # two ids leave the candidate set
    live = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert set(live["entry_ids"]) < set(figures["entry_ids"]), (
        "the shape this test needs: a STRICT shrink, which a subset check would accept"
    )
    before = _world()

    rejected = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                       resultingBalance=5, previewEntryIds=figures["entry_ids"])
    assert rejected.status_code == 400, rejected.get_json()
    assert _error_code(rejected) == "MERGE_PREVIEW_STALE"
    assert _world() == before
    _refresh()
    assert UserAddress.query.get(a.id).address_group_id is None
    assert UserAddress.query.get(b.id).address_group_id is None

    # ...and the SAME merge with the LIVE id list is accepted, so the rejection
    # above is the guard firing rather than the merge being impossible.
    accepted = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                       resultingBalance=5, previewEntryIds=live["entry_ids"])
    assert accepted.status_code == 201, accepted.get_json()
    group_id = accepted.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("5.00") == _place_ledger_sum(group_id)


def test_the_membership_fence_answers_BEFORE_the_staleness_guard(client, db, admin_auth_headers):
    """The guard ORDER, pinned separately from the guard itself.

    `_assert_place_group_eligible` runs before `_validate_merge_review`, so an
    already-grouped joiner is PLACE_GROUP_ADDRESS_ALREADY_GROUPED even when the
    preview is ALSO stale. Both facts matter: the caller is told the thing it
    can act on, and a test aiming at staleness must not build its scenario by
    grouping an address (it would then pass on this code instead).
    """
    ua, ub, uc = _customer(), _customer(), _customer()
    a, b, c = _address(ua, "A"), _address(ub, "B"), _address(uc, "C")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    _seed(uc, c, 1)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    elsewhere = _create(client, admin_auth_headers, [a.id, c.id], reason="a different place")
    assert elsewhere.status_code == 201, elsewhere.get_json()
    _seed(ub, b, 7)                                  # ...and the preview is stale too
    before = _world()

    rejected = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                       resultingBalance=5, previewEntryIds=figures["entry_ids"])
    assert rejected.status_code == 400, rejected.get_json()
    assert _error_code(rejected) == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"
    assert _world() == before


def test_previewEntryIds_are_compared_as_a_SET_not_as_an_ordered_list(client, db, admin_auth_headers):
    """The preview is ordered by (occurred_at, id), which is NOT necessarily
    ascending id. A list comparison would reject every merge containing a
    backdated entry — and since the ids never change, retrying fails FOR EVER."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    backdated = _seed(ua, a, 2)
    backdated.occurred_at = datetime.now(UTC) - timedelta(days=7)
    _db.session.commit()

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert figures["entry_ids"] != sorted(figures["entry_ids"]), "the shape this test needs"

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 9",
                      resultingBalance=9, previewEntryIds=sorted(figures["entry_ids"]))
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("9.00") == _place_ledger_sum(group_id)


def test_previewEntryIds_sent_ALONE_arm_the_guard_but_write_no_correction(
    client, db, admin_auth_headers
):
    """"Looking is not deciding." `_validate_merge_review` returns early only
    when there is no review AND no ids, while `_apply_merge_review` returns
    when there is no review — an asymmetry a cleanup refactor collapses in one
    direction or the other, so BOTH sides are pinned. Note the deliberate
    consequence: convergence is NOT claimed for a non-decision."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="just looking",
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _rows_of("merge_backfill:") == []
    assert _rows_of("merge_correction:") == []
    assert _place_balance(a.id) == Decimal("20.00")
    assert _place_ledger_sum(group_id) == Decimal("0.00")     # drift preserved by design
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert "resulting_balance" not in event.event_metadata
    assert "excluded_ledger_entry_ids" not in event.event_metadata

    # ...and the SAME payload with a stale id list is refused outright.
    uc, ud = _customer(), _customer()
    c, d = _address(uc, "C"), _address(ud, "D")
    _seed(uc, c, 1)
    stale_ids = _ok_preview(client, admin_auth_headers, [c.id, d.id])["entry_ids"]
    _seed(ud, d, 1)
    before = _world()
    rejected = _create(client, admin_auth_headers, [c.id, d.id], reason="just looking",
                       previewEntryIds=stale_ids)
    assert rejected.status_code == 400, rejected.get_json()
    assert _error_code(rejected) == "MERGE_PREVIEW_STALE"
    assert _world() == before


def test_previewEntryIds_of_an_EMPTY_merge_is_accepted_not_treated_as_absent(
    client, db, admin_auth_headers
):
    """`preview_entry_ids is None` versus falsiness. An `if preview_entry_ids:`
    would silently disarm the guard for exactly the empty-merge case — which is
    the address-24 shape."""
    a = _address(_customer(), "A")
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert figures["entry_ids"] == []

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="new office, 9 crates",
                      resultingBalance=9, previewEntryIds=[])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("9.00") == _place_ledger_sum(group_id)


def test_a_rejected_merge_leaves_no_flushed_AddressGroup_for_the_next_commit_to_adopt(
    client, db, admin_auth_headers
):
    """Validation runs before the group row exists precisely so a rejected
    review cannot leave a flushed AddressGroup behind. Moving the creation
    above the validation makes a rejected merge materialise on the NEXT commit,
    silently grouping two customers."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    _seed(ub, b, 3)                                  # invalidates the preview

    groups_before = AddressGroup.query.count()
    rejected = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 5",
                       resultingBalance=5, previewEntryIds=figures["entry_ids"])
    assert rejected.status_code == 400, rejected.get_json()

    # An unrelated successful write on the SAME process afterwards.
    _seed(ua, a, 1)
    _refresh()
    assert AddressGroup.query.count() == groups_before
    assert UserAddress.query.get(a.id).address_group_id is None
    assert UserAddress.query.get(b.id).address_group_id is None
    assert CustomerLinkEvent.query.count() == 0


# =========================================================================== #
# 6. MALFORMED INPUT — 400s, never 500s, never a silent reinterpretation.
# =========================================================================== #

@pytest.mark.parametrize(
    "stated",
    ["abc", "", True, False, [], {"a": 1}, "NaN", "Infinity", "-Infinity", "1.2.3", " "],
)
def test_a_malformed_resultingBalance_is_a_400_and_moves_nothing(
    client, db, admin_auth_headers, stated
):
    """Decimal("NaN") constructs happily and EVERY comparison against NaN is
    False, so an unguarded NaN sails past `delta != 0` straight into
    `bottle_ledger.quantity`. `false` is nastier still: Decimal(str(False))
    raises InvalidOperation, which IS an ArithmeticError — only that catch
    keeps it a 400 rather than a 500."""
    (ua, a), (ub, b) = _group_9_shape()
    before = _world()

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r", resultingBalance=stated)
    assert resp.status_code == 400, (stated, resp.status_code, resp.get_json())
    assert _error_code(resp) is None, "§13 defines no code here; the four §7.4 codes stay reserved"
    assert _world() == before
    # The join was REJECTED, so A and B are still two separate places holding
    # their own figures (6 and 5−4) — not one 7-bottle place.
    assert _place_balance(a.id) == Decimal("6.00")
    assert _place_balance(b.id) == Decimal("1.00")
    _refresh()
    assert UserAddress.query.get(a.id).address_group_id is None


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_the_JSON_literals_NaN_and_Infinity_are_rejected_too(client, db, admin_auth_headers, literal):
    """Python's own JSON parser ACCEPTS these literals — they are reachable, not
    theoretical."""
    (ua, a), (ub, b) = _group_9_shape()
    before = _world()
    body = '{"addressIds": [%d, %d], "reason": "r", "resultingBalance": %s}' % (a.id, b.id, literal)

    resp = client.post(CREATE, data=body, content_type="application/json",
                       headers={k: v for k, v in admin_auth_headers.items() if k != "Content-Type"})
    assert resp.status_code == 400, resp.get_json()
    assert _world() == before


@pytest.mark.parametrize("stated,expected", [("5", 5), (5.0, 5), ("5.00", 5), (0, 0), (-0.0, 0)])
def test_wellformed_but_unusual_resultingBalance_spellings_all_land_on_the_same_number(
    client, db, admin_auth_headers, stated, expected
):
    """Both halves re-coerce INDEPENDENTLY. If one reads a raw float and the
    other a Decimal, "5" becomes 4.999999999999999 in exactly one of them."""
    (ua, a), (ub, b) = _group_9_shape()
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted",
                      resultingBalance=stated, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal(expected).quantize(Decimal("0.01"))
    assert _place_ledger_sum(group_id) == Decimal(expected).quantize(Decimal("0.01"))
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert Decimal(event.event_metadata["resulting_balance"]) == Decimal(expected)


@pytest.mark.parametrize("excluded", [["abc"], [None], 5, {"a": 1}, [[]], [{}]])
def test_a_malformed_excludedLedgerEntryIds_is_a_400_not_a_crash(
    client, db, admin_auth_headers, excluded
):
    """`[int(v) for v in values]` raises TypeError for a non-iterable and
    ValueError for garbage; both must be caught. A 500 here is a bare-except
    swallow of a plain client error."""
    (ua, a), (ub, b) = _group_9_shape()
    before = _world()

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                   excludedLedgerEntryIds=excluded)
    assert resp.status_code == 400, (excluded, resp.status_code, resp.get_json())
    assert "ledger entry ids" in _error_text(resp)
    assert _world() == before


def test_a_string_member_of_excludedLedgerEntryIds_is_accepted_as_an_id(
    client, db, admin_auth_headers
):
    """The deliberate string-int tolerance: `["41"]` means entry 41."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                      excludedLedgerEntryIds=[str(e1.id)], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("3.00") == _place_ledger_sum(group_id)
    assert [Decimal(str(r.quantity)) for r in _rows_of("merge_exclude:")] == [Decimal("-4.00")]
    _refresh()
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.event_metadata["excluded_ledger_entry_ids"] == [e1.id], (
        "the audit record carries the INT id, not the string it arrived as"
    )


# --------------------------------------------------------------------------- #
# SUSPECTED BUGS — each demonstrated, none fixed (spec: report, do not fix).
#
#   BUG 1  FIXED — admin.py `[int(a) for a in address_ids]` inside the route
#          try block turned a malformed addressIds into a 500 (both join
#          routes); the coercion is now `_coerce_address_id_list`, a 400.
#   BUG 2  FIXED — CustomerLinkService._coerce_id_list iterated a STRING
#          character by character: excludedLedgerEntryIds "12" became [1, 2]
#          and reversed two real entries. Any non-list/tuple is now a 400.
#   BUG 3  FIXED — _coerce_id_list silently truncated floats and accepted
#          booleans: [1.9] and [True] both became entry id 1. Both are now
#          refused rather than truncated (bool checked BEFORE int).
#   BUG 4  FIXED — admin.py `request.args.get("group_id", type=int)` returned
#          None on a conversion failure, so ?group_id=abc silently previewed a
#          DIFFERENT merge (the create preview) under the add-to-group label;
#          `_parse_int_arg` now refuses it.
#   BUG 5  FIXED — both join routes handed the raw JWT identity STRING to a
#          parameter typed `int`, so `entry_metadata["acting_admin_id"]` was
#          "1" while the same row's `actor_user_id` column was 1;
#          `_acting_admin_id()` normalises it at the boundary.
#   BUG 6  FIXED — an out-of-range `resultingBalance` ("1e400") passed
#          `_coerce_resulting_balance` and only failed at the Numeric(12,2)
#          column, so a client error surfaced as a 500 (§12, on real Postgres).
#          The coercion now bounds the magnitude at the column's own
#          scale/precision and 400s. Pinned by its exact status in section 12.
#   BUG 8  TWO CONCURRENT JOINS OF THE SAME ADDRESS BOTH COMMIT (section 12,
#          real Postgres). `_load_addresses` takes no row lock, so both
#          transactions pass `_assert_place_group_eligible`, both absorb the
#          address, and the second membership write silently overwrites the
#          first — leaving the address's ledger history in one place and its
#          membership in another. Bottles stay conserved GLOBALLY and every
#          place still satisfies `balance == ledger_sum`, which is why no
#          oracle in this suite saw it until the attribution was asserted.
#
# The §7.4 design hole that BALANCE-DECOUPLED rows were exclusion-eligible is
# CLOSED, and both halves stay in §8:
# `test_a_previously_written_merge_backfill_is_REFUSED_as_an_exclusion_candidate`
# pins the refusal and `test_BUG_excluding_a_ledger_only_backfill_*` pins the
# property that motivated it (the place keeps the bottles it holds). A
# `merge_correction` is balance-COUPLED and stays excludable — the fence reads
# the row's coupling, not its place-level-ness.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("address_ids", [["abc", 2], [None, 2], [{}, 2], [[], 2]])
def test_BUG_the_create_route_returns_400_not_500_for_a_malformed_addressIds(
    client, db, admin_auth_headers, address_ids
):
    """FIXED — the strict xfail is gone.

    WAS: `[int(a) for a in address_ids]` sat inside the route's try block and
    raised ValueError/TypeError, which no `except ValidationError` catches, so
    the bare `except Exception` returned a 500 for a plain client error.

    NOW the coercion is `_coerce_address_id_list`, which raises the
    `ValidationError` the route's existing arm already forwards as a 400.
    """
    resp = client.post(CREATE, json={"addressIds": address_ids, "reason": "r"},
                       headers=admin_auth_headers)
    assert resp.status_code == 400, f"got {resp.status_code}: {resp.get_json()}"


def test_BUG_the_add_route_returns_400_not_500_for_a_malformed_addressIds(
    client, db, admin_auth_headers
):
    """FIXED — same defect, other join route, same shared coercion helper."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    resp = _add(client, admin_auth_headers, group_id, ["abc"], reason="r")
    assert resp.status_code == 400, f"got {resp.status_code}: {resp.get_json()}"


def test_BUG_a_string_excludedLedgerEntryIds_is_iterated_character_by_character(
    client, db, admin_auth_headers
):
    """Built so entry ids 1 and 2 really are in the merge — which is when the
    defect stops being a confusing 400 and starts moving real bottles."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    e2 = _seed(ub, b, 3)
    assert (e1.id, e2.id) == (1, 2), "this test needs the first two ledger rows in the DB"

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                   excludedLedgerEntryIds="12")
    assert resp.status_code == 400, (
        f"a string is not a list of ids; got {resp.status_code}: {resp.get_json()}"
    )


def test_a_string_excludedLedgerEntryIds_no_longer_reverses_the_entries_it_SPELLS(
    client, db, admin_auth_headers
):
    """UPDATED — this used to pin the HARM of BUG 2 and now pins its ABSENCE.

    It used to assert that `"12"`, iterated into `[1, 2]`, reversed BOTH real
    entries of this merge (`merge_exclude:` rows of -4.00 and -3.00) and left
    the place at 0.00 with the two ids on the audit event — seven bottles the
    place physically holds, written out of it by a client that sent one
    malformed field. `_coerce_id_list` now refuses any non-list/tuple before
    anything is iterated, so the whole episode is a 400 and NOTHING is written.

    The full preview is still fetched and passed, so the request is rejected on
    the malformed field alone rather than on staleness, and the id-space
    precondition is still asserted: if entries 1 and 2 ever stopped being real
    entries of this merge, the refusal would be uninteresting."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    e2 = _seed(ub, b, 3)
    assert (e1.id, e2.id) == (1, 2), "this test needs the first two ledger rows in the DB"
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    before = _world()

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                   excludedLedgerEntryIds="12", previewEntryIds=figures["entry_ids"])
    assert resp.status_code == 400, resp.get_json()
    assert "ledger entry ids" in _error_text(resp)

    assert _rows_of("merge_exclude:") == []
    _refresh()
    assert _world() == before, "a refused merge must leave the database exactly as it was"
    assert Decimal(str(BottleLedger.query.get(e1.id).quantity)) == Decimal("4.00")
    assert Decimal(str(BottleLedger.query.get(e2.id).quantity)) == Decimal("3.00")
    assert CustomerLinkEvent.query.filter_by(event_type="create_place_group").count() == 0


@pytest.mark.parametrize("excluded", [[1.9], [True]])
def test_BUG_a_fractional_or_boolean_exclusion_id_is_silently_truncated_to_entry_1(
    client, db, admin_auth_headers, excluded
):
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    assert e1.id == 1, "this test needs entry id 1 to be a real entry in the merge"

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                   excludedLedgerEntryIds=excluded)
    assert resp.status_code == 400, (
        f"{excluded!r} is not a list of ledger entry ids; got {resp.status_code}: {resp.get_json()}"
    )


@pytest.mark.parametrize("excluded", [[1.9], [True]])
def test_a_fractional_or_boolean_exclusion_id_no_longer_destroys_entry_1(
    client, db, admin_auth_headers, excluded
):
    """UPDATED — this used to pin the HARM of BUG 3 and now pins its ABSENCE.

    It used to assert that `int(1.9)` and `int(True)` both named entry 1 — a
    real four-bottle adjustment in this merge — which was reversed
    (`merge_exclude:` of -4.00), dropping the place from 7 to 3 and recording
    the id on the audit event. `_coerce_id_list` now refuses a float or a
    boolean member instead of truncating it, so the episode is a 400 and entry
    1 keeps its four bottles."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    assert e1.id == 1, "this test needs entry id 1 to be a real entry in the merge"
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    before = _world()

    resp = _create(client, admin_auth_headers, [a.id, b.id], reason="r",
                   excludedLedgerEntryIds=excluded, previewEntryIds=figures["entry_ids"])
    assert resp.status_code == 400, resp.get_json()
    assert "ledger entry ids" in _error_text(resp)

    assert _rows_of("merge_exclude:") == []
    _refresh()
    assert _world() == before, "a refused merge must leave the database exactly as it was"
    assert Decimal(str(BottleLedger.query.get(e1.id).quantity)) == Decimal("4.00")
    assert CustomerLinkEvent.query.filter_by(event_type="create_place_group").count() == 0


def test_BUG_a_malformed_group_id_silently_changes_which_merge_is_previewed(
    client, db, admin_auth_headers
):
    """FIXED — the strict xfail is gone.

    WAS: `request.args.get('group_id', type=int)` returned None on a conversion
    failure, so `?group_id=abc` silently returned the CREATE preview (different
    candidate set, different drift, different projection) while the panel
    labelled it the add-to-group preview.

    NOW `_parse_int_arg` distinguishes "absent" from "unreadable" and refuses
    the latter with a 400.
    """
    (ua, a), (ub, b) = _group_9_shape()
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    uc = _customer()
    c = _address(uc, "C")
    _seed(uc, c, 5)

    real = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    resp = _preview(client, admin_auth_headers, [c.id], raw_group_id="abc")
    assert resp.status_code == 400, (
        "a malformed group_id must not be silently dropped; it produced "
        f"{resp.status_code} with figures {resp.get_json().get('data')} while the real "
        f"add-preview reports computed={real['computed_balance']}"
    )


def test_a_malformed_group_id_no_longer_answers_a_DIFFERENT_merge(
    client, db, admin_auth_headers
):
    """WAS the harm-pin of BUG 4, now the proof it is closed.

    `?group_id=abc` used to succeed with the CREATE preview — a different
    candidate set with a different `computed_balance` (5 vs 12), a different
    `drift` and a different `projected_place_balance` — under a dialog the
    panel had already labelled "add to place 9", byte-identical to the
    no-group preview.

    It is now a 400 that names the offending argument, and the real
    add-to-group preview beside it is unchanged: the figures below are the ones
    the fix must NOT move."""
    (ua, a), (ub, b) = _group_9_shape()
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    uc = _customer()
    c = _address(uc, "C")
    _seed(uc, c, 5)

    real = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    assert (real["computed_balance"], real["stored_balance"], len(real["entry_ids"])) == (12, 12, 4)

    resp = _preview(client, admin_auth_headers, [c.id], raw_group_id="abc")
    assert resp.status_code == 400, resp.get_json()
    assert "group_id" in _error_text(resp)
    # The CREATE preview it used to be silently answered with is still a
    # perfectly valid request — it just has to be ASKED for.
    create_preview = _ok_preview(client, admin_auth_headers, [c.id])
    assert (create_preview["computed_balance"], create_preview["stored_balance"],
            len(create_preview["entry_ids"])) == (5, 5, 1)
    assert create_preview != real, "the two previews are different merges — that was the point"


# =========================================================================== #
# 7. THE REASON FENCE (service level — the routes pre-reject an empty reason).
# =========================================================================== #

@pytest.mark.parametrize("kwargs", [
    {"resulting_balance": 5},
    {"excluded_ledger_entry_ids": ["placeholder"]},
])
def test_a_review_without_a_reason_is_MERGE_REASON_REQUIRED_at_the_service(db, kwargs):
    """Both HTTP routes reject an empty reason FIRST with a code-less 400, so
    this fence is only reachable at the service — which is where it belongs,
    because `create_address_group` (the legacy canonical route) still passes an
    empty reason for a plain join."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    _seed(ub, b, 3)
    if kwargs.get("excluded_ledger_entry_ids") == ["placeholder"]:
        kwargs = {"excluded_ledger_entry_ids": [e1.id]}
    before = _world()

    for reason in ("", "   "):
        with pytest.raises(ValidationError) as exc:
            CustomerLinkService().create_place_group(
                [a.id, b.id], acting_admin_id=ua.id, reason=reason, **kwargs
            )
        assert exc.value.error_code == "MERGE_REASON_REQUIRED"
        _db.session.rollback()
    assert _world() == before


def test_a_plain_join_still_needs_no_reason_at_the_service(db):
    """The fence applies to CORRECTIONS only."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)

    group = CustomerLinkService().create_place_group([a.id, b.id], acting_admin_id=ua.id, reason="")
    assert group.id is not None
    assert _place_balance(a.id) == Decimal("7.00") == _place_ledger_sum(group.id)
    assert _rows_of("merge_backfill:") == _rows_of("merge_correction:") == []


# =========================================================================== #
# 8. PLACE-LEVEL ROWS IN A LATER PREVIEW — attribution, prefills, customer view.
# =========================================================================== #

def _repaired_place(client, headers, stated=12):
    """A drifted place repaired by a reviewed merge — the starting point for
    every "what happens NEXT" test below."""
    u_a, a = _address_24_shape()
    u_b = _customer()
    b = _address(u_b, "B")
    figures = _ok_preview(client, headers, [a.id, b.id])
    created = _create(client, headers, [a.id, b.id], reason="counted 12 crates",
                      resultingBalance=stated, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    return created.get_json()["data"]["place_group_id"], (u_a, a), (u_b, b)


def test_a_previously_written_merge_backfill_is_REFUSED_as_an_exclusion_candidate(
    client, db, admin_auth_headers
):
    """UPDATED — this used to pin the design hole and now pins its closure.

    It used to assert that excluding a previously written `merge_backfill`
    SUCCEEDED (200) and appended a balance-COUPLED reversal of -20.00, dropping
    a place that physically holds 12 crates to -8.00 on both figures.

    `build_merge_preview` still returns every group-scoped row, so the panel can
    still show the backfill — what changed is that the COMMITTER now reads the
    row's COUPLING, not just its id: a `merge_backfill` moved the LEDGER ONLY,
    so it is not an exclusion candidate at all and the whole episode is refused
    with `MERGE_EXCLUSION_NOT_ELIGIBLE`. The place keeps its 12, the join does
    not happen, and the way to change what the place holds remains
    `resultingBalance`, which is measured post-exclusion.
    """
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    assert _place_balance(a.id) == Decimal("12.00")

    uc = _customer()
    c = _address(uc, "C")
    figures = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    backfill_row = next(r for r in figures["entries"]
                        if (r["idempotency_key"] or "").startswith("merge_backfill:"))
    assert backfill_row["quantity"] == 20
    before = _world()

    added = _add(client, admin_auth_headers, group_id, [c.id], reason="new hire",
                 excludedLedgerEntryIds=[backfill_row["id"]],
                 previewEntryIds=figures["entry_ids"])
    assert added.status_code == 400, added.get_json()
    assert _error_code(added) == "MERGE_EXCLUSION_NOT_ELIGIBLE", added.get_json()

    # Nothing was reversed and nothing joined.
    assert _rows_of("merge_exclude:") == []
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)
    _refresh()
    assert _world() == before, "a refused merge must leave the database exactly as it was"
    assert UserAddress.query.get(c.id).address_group_id is None


def test_BUG_excluding_a_ledger_only_backfill_must_not_move_the_place_balance(
    client, db, admin_auth_headers
):
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    assert _place_balance(a.id) == Decimal("12.00")

    uc = _customer()
    c = _address(uc, "C")
    figures = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    backfill_row = next(r for r in figures["entries"]
                        if (r["idempotency_key"] or "").startswith("merge_backfill:"))

    added = _add(client, admin_auth_headers, group_id, [c.id], reason="new hire",
                 excludedLedgerEntryIds=[backfill_row["id"]],
                 previewEntryIds=figures["entry_ids"])
    assert added.status_code in (200, 400), added.get_json()
    assert _place_balance(a.id) == Decimal("12.00"), (
        "reversing a balance-DECOUPLED row must not take 20 real bottles out of the place"
    )


def test_a_previously_written_merge_correction_can_be_excluded_and_convergence_survives(
    client, db, admin_auth_headers
):
    """A `merge_correction` IS balance-coupled, so its reversal is
    arithmetically consistent — it simply undoes an earlier admin's counted
    number, with nothing in the row to say it was a correction rather than a
    movement. Pinned including the convergence guarantee."""
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    uc = _customer()
    c = _address(uc, "C")
    figures = _ok_preview(client, admin_auth_headers, [c.id], group_id=group_id)
    correction_row = next(r for r in figures["entries"]
                          if (r["idempotency_key"] or "").startswith("merge_correction:"))
    assert correction_row["quantity"] == -8

    added = _add(client, admin_auth_headers, group_id, [c.id], reason="undo the count",
                 excludedLedgerEntryIds=[correction_row["id"]],
                 previewEntryIds=figures["entry_ids"])
    assert added.status_code == 200, added.get_json()
    assert _place_balance(a.id) == Decimal("20.00") == _place_ledger_sum(group_id)


def test_a_second_review_restating_the_same_number_is_idempotent(client, db, admin_auth_headers):
    """The property that distinguishes this design from the coupled one: a
    SEQUENCE of previews converges instead of chasing itself."""
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    backfills = len(_rows_of("merge_backfill:"))
    corrections = len(_rows_of("merge_correction:"))

    ud = _customer()
    d = _address(ud, "D")
    figures = _ok_preview(client, admin_auth_headers, [d.id], group_id=group_id)
    assert figures["drift"] == 0
    assert figures["projected_place_balance"] == 12

    added = _add(client, admin_auth_headers, group_id, [d.id], reason="still 12",
                 resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert added.status_code == 200, added.get_json()
    assert len(_rows_of("merge_backfill:")) == backfills
    assert len(_rows_of("merge_correction:")) == corrections
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)


def test_add_addresses_to_group_produces_byte_identical_arithmetic_to_create(
    client, db, admin_auth_headers
):
    """Both halves are shared verbatim, but the preview's `group_id` argument
    differs (None vs G). A wrong group_id at either call site makes the
    candidate set — and so `ledger_sum_before` — wrong for exactly one route.

    Asserted by RUNNING BOTH: the same merge (a 20-bottle drifted place plus a
    5-bottle clean one, stated 12) is committed once through CREATE and once
    through ADD, and the two must produce the same backfill and the same
    correction. Hard-coding one route's numbers would pass while the other
    route's group_id was wrong."""
    def figures_and_quantities(build):
        group_id, entry_ids = build()
        return (
            entry_ids,
            [Decimal(str(e.quantity)) for e in _rows_of(f"merge_backfill:{group_id}:")],
            [Decimal(str(e.quantity)) for e in _rows_of(f"merge_correction:{group_id}:")],
            _place_balance_of_group(group_id),
            _place_ledger_sum(group_id),
        )

    def via_create():
        u_a, a = _address_24_shape()
        u_p = _customer()
        p = _address(u_p, "P")
        _seed(u_p, p, 5)
        figures = _ok_preview(client, admin_auth_headers, [a.id, p.id])
        assert (figures["stored_balance"], figures["computed_balance"], figures["drift"]) == (25, 5, 20)
        created = _create(client, admin_auth_headers, [a.id, p.id], reason="counted 12",
                          resultingBalance=12, previewEntryIds=figures["entry_ids"])
        assert created.status_code == 201, created.get_json()
        return created.get_json()["data"]["place_group_id"], figures["entry_ids"]

    def via_add():
        u_a, a = _address_24_shape()
        u_f, u_g = _customer(), _customer()
        f, g = _address(u_f, "F"), _address(u_g, "G")
        seeded = _create(client, admin_auth_headers, [f.id, g.id], reason="existing office")
        assert seeded.status_code == 201, seeded.get_json()
        group_id = seeded.get_json()["data"]["place_group_id"]
        _seed(u_f, f, 5)                              # 5 bottles, group-scoped
        figures = _ok_preview(client, admin_auth_headers, [a.id], group_id=group_id)
        assert (figures["stored_balance"], figures["computed_balance"], figures["drift"]) == (25, 5, 20)
        added = _add(client, admin_auth_headers, group_id, [a.id], reason="counted 12",
                     resultingBalance=12, previewEntryIds=figures["entry_ids"])
        assert added.status_code == 200, added.get_json()
        assert _detail(client, admin_auth_headers, group_id)["place_balance"] == 12
        return group_id, figures["entry_ids"]

    created_ids, c_backfill, c_correction, c_place, c_ledger = figures_and_quantities(via_create)
    added_ids, a_backfill, a_correction, a_place, a_ledger = figures_and_quantities(via_add)

    assert c_backfill == a_backfill == [Decimal("20.00")]
    # stated 12 against a post-exclusion basis of 25 (20 drifted + 5 clean).
    assert c_correction == a_correction == [Decimal("-13.00")]
    assert c_place == a_place == Decimal("12.00")
    assert c_ledger == a_ledger == Decimal("12.00")
    # The candidate sets differ in SHAPE (one own-scope row vs one group-scoped
    # row) yet both are exactly one entry — the 5-bottle seed, never the drifted
    # place's absent history.
    assert len(created_ids) == len(added_ids) == 1


def test_a_review_of_a_place_that_never_moved_a_bottle_anchors_on_the_lowest_joining_address(
    client, db, admin_auth_headers
):
    """`_place_correction_anchor` falls back to min(addresses) only when the
    preview has no entries — a bare min(entries) would 500 on exactly this
    case, which is the commonest real one for a new office."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    lowest = min((a, b), key=lambda x: x.id)
    lowest_owner = ua if lowest.id == a.id else ub

    created = _create(client, admin_auth_headers, [a.id, b.id],
                      reason="new office, 9 crates on site", resultingBalance=9)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert _place_balance(a.id) == Decimal("9.00") == _place_ledger_sum(group_id)
    assert _rows_of("merge_backfill:") == []
    correction = _rows_of("merge_correction:")[0]
    assert (correction.user_id, correction.address_id) == (lowest_owner.id, lowest.id)


def test_the_correction_anchor_is_the_LOWEST_ID_entry_not_the_chronologically_first(
    client, db, admin_auth_headers
):
    """Determinism is the whole point: two identical calls must never attribute
    to two different coworkers."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    low = _seed(ua, a, 4)                    # lowest id
    high = _seed(ub, b, 3)                   # highest id, but backdated below
    high.occurred_at = datetime.now(UTC) - timedelta(days=10)
    _db.session.commit()

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    assert figures["entry_ids"][0] == high.id, "chronologically first is the HIGH id"

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 10",
                      resultingBalance=10, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    correction = _rows_of("merge_correction:")[0]
    assert (correction.user_id, correction.address_id) == (ua.id, a.id)


def test_a_place_level_correction_does_not_inflate_a_members_departure_prefill(
    client, db, admin_auth_headers
):
    """The correction BORROWS a member's (user_id, address_id) because the
    columns are NOT NULL. Counting it would inflate exactly one coworker's
    departure default by the whole place-level correction, and an admin
    accepting that default splits the place's bottles onto the wrong person."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 6)
    _seed(ub, b, 5)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 21",
                      resultingBalance=21, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    correction = _rows_of("merge_correction:")[0]
    assert correction.quantity == Decimal("10.00")
    assert correction.address_id == a.id, "the correction is stamped on the lowest-id entry"
    assert _place_balance(a.id) == Decimal("21.00") == _place_ledger_sum(group_id)

    assert BottleTrackingService.suggested_bottles_leaving(group_id, a.id) == Decimal("6.00")
    assert BottleTrackingService.suggested_bottles_leaving(group_id, b.id) == Decimal("5.00")
    detail = _detail(client, admin_auth_headers, group_id)
    # Members are flat rows keyed by `address_id` (there is no nested "address"
    # object, and no per-member balance at all — the place holds one pool).
    by_address = {m["address_id"]: m["suggested_bottles_leaving"] for m in detail["members"]}
    assert by_address == {a.id: 6, b.id: 5}
    assert all("balance" not in m for m in detail["members"])
    assert detail["place_balance"] == 21, "the pool itself is the admin's stated 21"


def test_a_merge_exclude_reversal_IS_still_counted_in_the_departure_prefill(
    client, db, admin_auth_headers
):
    """PLACE_LEVEL_LEDGER_KEY_PREFIXES deliberately OMITS merge_exclude: a
    reversal is attributed to the very entry it neutralises. Adding it "for
    symmetry" leaves the excluded quantity in the prefill."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 6)
    _seed(ub, b, 5)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="never happened",
                      excludedLedgerEntryIds=[e1.id], previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    assert BottleTrackingService.suggested_bottles_leaving(group_id, a.id) == Decimal("0.00")
    assert BottleTrackingService.suggested_bottles_leaving(group_id, b.id) == Decimal("5.00")


def test_place_level_rows_are_not_shown_to_a_customer_as_their_own(client, db, admin_auth_headers):
    """This view suppresses `notes`, so a leftover attribution shows one
    coworker an unexplained +/-N flagged as theirs — a number they did not
    cause and cannot account for."""
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    _seed(u_a, a, 3, notes="a real movement of A's own")
    _refresh()

    rows = BottleLedger.query.filter(BottleLedger.address_group_id == group_id).all()
    place_level = [r for r in rows
                   if (r.entry_metadata or {}).get("source") in ("merge_backfill", "merge_correction")]
    assert len(place_level) == 2
    for entry in place_level:
        assert entry.user_id == u_a.id, "the borrowed stamp is what makes this test meaningful"
        view = serialize_customer_place_ledger_entry(entry, viewer_user_id=u_a.id)
        assert view["member_name"] is None
        assert view["is_own"] is False
        assert set(view) == {"id", "address_id", "event_type", "quantity", "occurred_at",
                             "order_id", "order_number", "member_name", "is_own"}

    own = next(r for r in rows if (r.entry_metadata or {}).get("source") == "admin_adjustment")
    own_view = serialize_customer_place_ledger_entry(own, viewer_user_id=u_a.id)
    assert own_view["member_name"]
    assert own_view["is_own"] is True


# =========================================================================== #
# 9. THE AUDIT EVENT.
# =========================================================================== #

def test_the_join_event_records_the_review_decision_in_event_metadata(
    client, db, admin_auth_headers
):
    """`resulting_balance` is STRINGIFIED because JSON has no Decimal, and
    `if stated is not None` (not truthiness) is what keeps a stated ZERO on the
    record."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    e1 = _seed(ua, a, 4)
    e2 = _seed(ub, b, 3)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="empty office",
                      excludedLedgerEntryIds=[e2.id, e1.id],      # deliberately unsorted
                      resultingBalance=0, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    _refresh()
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.event_metadata["excluded_ledger_entry_ids"] == sorted([e1.id, e2.id])
    assert event.event_metadata["resulting_balance"] == "0"
    assert event.event_metadata["rescoped_ledger_entry_ids"] == sorted([e1.id, e2.id])
    assert event.reason.startswith(f"[group {group_id}] ")
    assert event.member_user_ids == sorted({ua.id, ub.id})
    assert event.acting_admin_id == User.query.filter_by(role=UserRole.ADMIN).one().id


def test_a_plain_join_records_no_review_keys_at_all(client, db, admin_auth_headers):
    """An always-present key with a null value makes "was this join reviewed?"
    unanswerable from the audit trail."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="just coworkers")
    assert created.status_code == 201, created.get_json()
    _refresh()
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert set(event.event_metadata) == {"rescoped_ledger_entry_ids"}


def test_member_user_ids_are_sorted_and_deduped_when_one_person_owns_two_addresses(
    client, db, admin_auth_headers
):
    """Both halves need a failure mode. DEDUP: one owner with two desks must
    appear once. SORTED: the addresses are handed over in DESCENDING owner
    order, so an order-preserving `list(dict.fromkeys(...))` would answer
    [high, low] and only a real sort answers [low, high]."""
    ua = _customer()                                     # lower user id
    ub = _customer()                                     # higher user id
    a1, a2 = _address(ua, "Desk1"), _address(ua, "Desk2")
    b1 = _address(ub, "Desk3")
    _seed(ua, a1, 4)
    assert ua.id < ub.id, "the shape this test needs"

    created = _create(client, admin_auth_headers, [b1.id, a2.id, a1.id],
                      reason="two people, three desks")
    assert created.status_code == 201, created.get_json()
    _refresh()
    event = CustomerLinkEvent.query.filter_by(event_type="create_place_group").one()
    assert event.member_user_ids == [ua.id, ub.id]       # deduped AND sorted


# =========================================================================== #
# 10. SNAPSHOTS, INTERPLAY, IDEMPOTENCE, LIFECYCLE.
# =========================================================================== #

def test_the_running_snapshots_are_rebuilt_over_the_whole_merged_timeline_after_the_corrections(
    client, db, admin_auth_headers
):
    """`recompute_balance_after` must run LAST. If it ran inside the absorb
    only, the correction rows would carry the balance_after they were born with
    and the history view would contradict the summary."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    _seed(ub, b, 5)
    _seed(ub, b, 2)
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    drop = figures["entry_ids"][0]

    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      excludedLedgerEntryIds=[drop], resultingBalance=12,
                      previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    _refresh()
    scope = BottleScope.for_group(group_id)
    rows = (BottleLedger.query.filter(*scope.ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    running = Decimal("0.00")
    for row in rows:
        running += Decimal(str(row.quantity))
        assert Decimal(str(row.balance_after)) == running, f"snapshot broke at entry {row.id}"
    assert running == _place_balance(a.id) == _place_ledger_sum(group_id) == Decimal("12.00")


def test_recompute_is_deterministic_across_reruns_for_same_timestamp_rows(
    client, db, admin_auth_headers
):
    """`occurred_at` alone is unstable for entries written inside ONE
    transaction (the exclusion and the correction can share a microsecond).
    An occurred_at-only ORDER BY makes balance_after flicker between reloads."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 5)
    service = BottleTrackingService()
    fine = service.issue_fine(user_id=ua.id, address_id=a.id, quantity=Decimal("1"),
                              fine_amount=Decimal("1000"), actor_user_id=ua.id)
    service.mark_fine_paid(fine.id, actor_user_id=ua.id)
    _db.session.commit()
    _seed(ub, b, 3)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 6",
                      excludedLedgerEntryIds=figures["entry_ids"][:1],
                      resultingBalance=6, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    _refresh()
    scope = BottleScope.for_group(group_id)
    first = {e.id: Decimal(str(e.balance_after))
             for e in BottleLedger.query.filter(*scope.ledger_filter()).all()}
    BottleTrackingService.recompute_balance_after(scope)
    _db.session.commit()
    _refresh()
    second = {e.id: Decimal(str(e.balance_after))
              for e in BottleLedger.query.filter(*scope.ledger_filter()).all()}
    assert first == second

    # ...and now with a REAL tie. `issue_fine` and `mark_fine_paid` are two
    # calls and two `_utc_now()` readings, so the pair this test is named after
    # does NOT actually share a timestamp as written — which left the original
    # assertion above with no tie to break at all. The tie is forced here (rows
    # sharing an `occurred_at` are ordinary: a coarse clock, a bulk import, two
    # halves stamped from one value), and the snapshots are then checked
    # against the documented (occurred_at, id) walk rather than only against a
    # rerun of themselves.
    _refresh()
    issued = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_ISSUED).one()
    paid = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_PAID).one()
    assert issued.id < paid.id
    paid.occurred_at = issued.occurred_at
    _db.session.commit()

    BottleTrackingService.recompute_balance_after(scope)
    _db.session.commit()
    _refresh()
    third = {e.id: Decimal(str(e.balance_after))
             for e in BottleLedger.query.filter(*scope.ledger_filter()).all()}
    BottleTrackingService.recompute_balance_after(scope)
    _db.session.commit()
    _refresh()
    assert third == {e.id: Decimal(str(e.balance_after))
                     for e in BottleLedger.query.filter(*scope.ledger_filter()).all()}

    rows = (BottleLedger.query.filter(*scope.ledger_filter())
            .order_by(BottleLedger.occurred_at.asc(), BottleLedger.id.asc()).all())
    assert [r.id for r in rows].index(issued.id) < [r.id for r in rows].index(paid.id), (
        "the tied pair must still walk in id order"
    )
    running = Decimal("0.00")
    for row in rows:
        running += Decimal(str(row.quantity))
        assert Decimal(str(row.balance_after)) == running, f"snapshot broke at entry {row.id}"
    assert running == _place_balance(a.id) == _place_ledger_sum(group_id) == Decimal("6.00")


def test_a_review_committed_while_an_order_is_in_flight_then_delivered(client, db, admin_auth_headers):
    """An in-flight order writes NOTHING to the ledger, so it does not make the
    preview stale — but the delivery lands ON TOP of the correction and the
    snapshot chain must not break at exactly the crate the driver handed over."""
    u_a, a = _address_24_shape()
    ub = _customer()
    b = _address(ub, "B")
    order = Order(
        user_id=u_a.id, order_number="ORD-INFLIGHT-1", status=OrderStatus.CONFIRMED,
        subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"), created_at=datetime.now(UTC),
    )
    _db.session.add(order)
    _db.session.commit()

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="counted 12",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)

    entry = BottleTrackingService().record_bottles_delivered(
        order_id=order.id, user_id=u_a.id, address_id=a.id,
        quantity=Decimal("3"), actor_user_id=u_a.id,
    )
    _db.session.commit()
    _refresh()
    assert _place_balance(a.id) == Decimal("15.00") == _place_ledger_sum(group_id)
    assert Decimal(str(BottleLedger.query.get(entry.id).balance_after)) == Decimal("15.00")


def test_committing_the_same_merge_twice_applies_the_correction_once(client, db, admin_auth_headers):
    """The idempotency keys are EPISODE-scoped on event.id, so a second episode
    would write a SECOND correction. The membership fence is the only thing
    preventing double application."""
    u_a, a = _address_24_shape()
    b = _address(_customer(), "B")
    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id])
    body = {"reason": "counted 12", "resultingBalance": 12,
            "previewEntryIds": figures["entry_ids"]}

    first = _create(client, admin_auth_headers, [a.id, b.id], **body)
    assert first.status_code == 201, first.get_json()
    group_id = first.get_json()["data"]["place_group_id"]

    second = _create(client, admin_auth_headers, [a.id, b.id], **body)
    assert second.status_code == 400, second.get_json()
    assert _error_code(second) == "PLACE_GROUP_ADDRESS_ALREADY_GROUPED"

    assert len(_rows_of("merge_backfill:")) == 1
    assert len(_rows_of("merge_correction:")) == 1
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)
    assert AddressGroup.query.count() == 1


def test_a_reviewed_merge_then_a_split_removal_then_the_dissolve(client, db, admin_auth_headers):
    """`release_group_history_to_address` re-stamps ONLY the survivor's own
    entries. Conservation must hold at EVERY step, the memberless AddressGroup
    row must survive (its ledger rows FK to it), and the survivor's stored
    balance must end up equal to its own-scope ledger sum."""
    u_a, a = _address_24_shape()
    u_b, u_c = _customer(), _customer()
    b, c = _address(u_b, "B"), _address(u_c, "C")
    _seed(u_b, b, 5)
    _seed(u_c, c, 4)

    figures = _ok_preview(client, admin_auth_headers, [a.id, b.id, c.id])
    created = _create(client, admin_auth_headers, [a.id, b.id, c.id], reason="counted 12",
                      resultingBalance=12, previewEntryIds=figures["entry_ids"])
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("12.00") == _place_ledger_sum(group_id)

    total_before = _all_balances()
    suggestion = BottleTrackingService.suggested_bottles_leaving(group_id, c.id)
    removed = client.delete(f"{CREATE}/{group_id}/addresses/{c.id}",
                            json={"reason": "left the company",
                                  "bottlesLeaving": float(suggestion)},
                            headers=admin_auth_headers)
    assert removed.status_code == 200, removed.get_json()
    assert removed.get_json()["data"]["dissolved"] is False
    assert _all_balances() == total_before, "a split MOVES bottles, it never mints them"
    assert _place_balance(c.id) == suggestion
    assert _place_balance(a.id) == Decimal("12.00") - suggestion
    # ...and the PAIR, per scope: a global sum alone is blind to a split that
    # lands the right total in the wrong place.
    assert _place_ledger_sum(group_id) == _place_balance(a.id)
    assert _own_ledger_sum(c.id) == _place_balance(c.id)

    dissolve = client.delete(f"{CREATE}/{group_id}/addresses/{b.id}",
                             json={"reason": "also left", "bottlesLeaving": 0},
                             headers=admin_auth_headers)
    assert dissolve.status_code == 200, dissolve.get_json()
    assert dissolve.get_json()["data"]["dissolved"] is True
    assert _all_balances() == total_before

    _refresh()
    assert AddressGroup.query.get(group_id) is not None, "the memberless row is KEPT (FK anchor)"
    assert UserAddress.query.get(a.id).address_group_id is None
    assert _place_balance(a.id) == _own_ledger_sum(a.id), (
        "the survivor's stored figure and its own-scope ledger must agree after the dissolve"
    )


def test_a_grouped_address_stays_undeletable_after_a_reviewed_merge(client, db, admin_auth_headers):
    """A merge review stamps ledger rows on MEMBER addresses (including borrowed
    place-level attributions). Deleting one would break
    `bottle_ledger.address_id`'s FK on Postgres while passing silently in the
    FK-off SQLite suite."""
    group_id, (u_a, a), (u_b, b) = _repaired_place(client, admin_auth_headers)
    with pytest.raises(ValidationError) as exc:
        CustomerLinkService.assert_address_not_in_place_group(a.id)
    assert exc.value.error_code == "PLACE_GROUP_ADDRESS_NOT_DELETABLE"
    _refresh()
    # EXACTLY the two borrowed place-level stamps, not "more than zero": A's own
    # history was wiped by the address-24 shape, so if the review ever stopped
    # anchoring on a member address this count would be 0 and a `> 0` assertion
    # would be the only thing standing between that and a dangling FK on
    # Postgres — where this suite, with FOREIGN KEYS off, would see nothing.
    rows = BottleLedger.query.filter_by(address_id=a.id).all()
    assert sorted((r.entry_metadata or {}).get("source") for r in rows) == \
        ["merge_backfill", "merge_correction"]
    assert all(r.address_group_id == group_id for r in rows)


def test_a_LIVE_place_whose_ledger_over_recorded_needs_a_NEGATIVE_backfill(
    client, db, admin_auth_headers
):
    """The signed backfill's NEGATIVE branch — exactly what a `max(0, drift)` kills.

    UPDATED: this used to reach the negative branch through a MEMBERLESS group.
    `release_group_history_to_address` re-stamps only the SURVIVOR's own rows out
    and then posts `-inherited`, so what stays behind on a dissolved group sums
    to exactly `ledger_sum - stored` — the place's drift, sign flipped — and
    re-populating that group needed a negative backfill to converge. A dissolved
    group is now refused as a join target (`PLACE_GROUP_DISSOLVED`), so that
    route to the branch is gone.

    The branch itself is not about dissolution at all: it is about a place whose
    LEDGER recorded MORE than the place holds, which a departed member's
    group-stamped rows produce on a perfectly LIVE place too. That is the shape
    used here — a three-member place, so the removal does not dissolve — and it
    is a better test of the branch for it.
    """
    ua, ub, uc = _customer(), _customer(), _customer()
    a, b, c = _address(ua, "A"), _address(ub, "B"), _address(uc, "C")
    _seed(ua, a, 10)
    _nudge_stored(a, "4")            # A's ledger recorded 10; A only holds 4
    _seed(ub, b, 4)
    created = _create(client, admin_auth_headers, [a.id, b.id, c.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    assert _place_balance(a.id) == Decimal("8.00")
    assert _place_ledger_sum(group_id) == Decimal("14.00")

    # A leaves. TWO members remain, so §7.3 does not dissolve — and A's own rows
    # deliberately stay group-scoped (§7.1), which is what over-records the
    # ledger against the figure the place actually holds.
    resp = client.delete(f"{CREATE}/{group_id}/addresses/{a.id}",
                         json={"reason": "left", "bottlesLeaving": 0},
                         headers=admin_auth_headers)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["dissolved"] is False
    _refresh()
    assert UserAddress.query.filter_by(address_group_id=group_id).count() == 2
    assert _place_balance(b.id) == Decimal("8.00")
    assert _place_ledger_sum(group_id) == Decimal("14.00"), (
        "A's own rows stay group-scoped; the place's ledger now over-records by 6"
    )

    ud = _customer()
    d = _address(ud, "D")
    _seed(ud, d, 5)
    figures = _ok_preview(client, admin_auth_headers, [d.id], group_id=group_id)
    assert figures["computed_balance"] == 19        # group ledger 14 + D's own 5
    assert figures["stored_balance"] == 13          # place 8 + D's own 5
    assert figures["drift"] == -6
    assert figures["projected_place_balance"] == 13

    added = _add(client, admin_auth_headers, group_id, [d.id], reason="counted 13",
                 resultingBalance=13, previewEntryIds=figures["entry_ids"])
    assert added.status_code == 200, added.get_json()
    backfills = _rows_of(f"merge_backfill:{group_id}:")
    assert [Decimal(str(r.quantity)) for r in backfills] == [Decimal("-6.00")], (
        "the negative half of the signed backfill"
    )
    assert _rows_of(f"merge_correction:{group_id}:") == [], "stating what it already holds is a no-op"
    assert _place_balance(d.id) == Decimal("13.00") == _place_ledger_sum(group_id)


# =========================================================================== #
# 11. AUTHORISATION AND THE ROUTE CONTRACT.
# =========================================================================== #

def test_a_MANAGER_can_preview_but_cannot_commit_and_an_OPERATOR_can_do_neither(
    client, db, app, admin_auth_headers
):
    """The read route is deliberately `view_users` and the mutations
    `manage_users`. A copy-pasted decorator hands every manager the ability to
    rewrite place balances."""
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    _seed(ua, a, 4)
    _seed(ub, b, 3)
    created = _create(client, admin_auth_headers, [a.id, b.id], reason="office")
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]
    uc = _customer()
    c = _address(uc, "C")

    manager = _headers_for(app, _staff(UserRole.MANAGER))
    operator = _headers_for(app, _staff(UserRole.OPERATOR))

    assert _preview(client, manager, [c.id], group_id=group_id).status_code == 200
    assert _preview(client, operator, [c.id], group_id=group_id).status_code == 403

    for headers, expected in ((manager, 403), (operator, 403)):
        assert _create(client, headers, [c.id, a.id], reason="r").status_code == expected
        assert _add(client, headers, group_id, [c.id], reason="r").status_code == expected
        assert client.delete(f"{CREATE}/{group_id}/addresses/{a.id}",
                             json={"reason": "r"}, headers=headers).status_code == expected

    _refresh()
    assert UserAddress.query.get(c.id).address_group_id is None
    assert _place_balance(a.id) == Decimal("7.00")


def test_a_plain_customer_jwt_reaches_none_of_the_merge_routes(client, db, auth_headers):
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")
    # 403, not 401: the JWT is valid, the ROLE is not. Pinned exactly — a
    # `in (401, 403)` range would hide a route that silently stopped
    # authenticating (401) or one whose permission gate vanished.
    assert _preview(client, auth_headers, [a.id, b.id]).status_code == 403
    assert _create(client, auth_headers, [a.id, b.id], reason="r").status_code == 403
    assert _add(client, auth_headers, 1, [a.id], reason="r").status_code == 403
    _refresh()
    assert AddressGroup.query.count() == 0


def test_an_unauthenticated_request_never_reaches_the_service(app, db):
    """A FRESH client: the session-scoped one leaks JWT cookies into 401 tests
    on this repo, and this test would then pass vacuously."""
    anonymous = app.test_client()
    ua, ub = _customer(), _customer()
    a, b = _address(ua, "A"), _address(ub, "B")

    assert anonymous.get(f"{PREVIEW}?address_ids={a.id},{b.id}").status_code == 401
    assert anonymous.post(CREATE, json={"addressIds": [a.id, b.id], "reason": "r"}).status_code == 401
    _refresh()
    assert AddressGroup.query.count() == 0
    assert CustomerLinkEvent.query.count() == 0


def test_the_api_contract_snapshot_still_lists_the_merge_preview_route(db):
    """Any accidental route addition or rename on this axis moves the snapshot,
    which is explicitly out of bounds for this work."""
    root = Path(__file__).resolve().parents[2]
    routes = json.loads((root / "tests/contract/snapshots/api_routes.json").read_text())
    # 557 -> 559: Plan E task A1.2 is the one task explicitly permitted to move
    # this snapshot (global constraint C8) and it added exactly two READ-ONLY
    # routes -- GET /api/v1/admin/place-groups
    # (admin.list_place_groups_admin) and GET /api/v1/admin/place-group-suggestions
    # (admin.list_place_group_suggestions). The merge-preview assertions below
    # are untouched and still pass; only this estate-wide count moved, and it
    # still fails on any FURTHER unannounced route change.
    #
    # 559 -> 560: this guard then DID fire on a further change, exactly as
    # intended, and the route is announced here rather than the count merely
    # bumped -- POST /api/v1/staff/operator/users/<int:user_id>/order-estimate
    # (staff.get_client_order_estimate). It exists because the operator screen
    # priced products for the CALLER (the operator) while the order was priced
    # for the CLIENT: a corporate-contract client showed 45,000 and was charged
    # 27,000. The estimate delegates to the same item-pricing loop
    # `create_phone_order` uses, so the figure shown and the figure charged are
    # ONE decision. Read-only; it creates no order.
    #
    # 560 -> 566: the admin Dispatch map backend (`admin_dispatch` blueprint)
    # added six new /api/v1/admin/dispatch/* routes -- GET .../snapshot, GET
    # .../routes/<int:driver_id>/geometry, PUT .../routes/<int:driver_id>/stops,
    # POST .../routes/<int:driver_id>/reoptimize, POST
    # .../stops/<int:delivery_id>/assign, POST
    # .../stops/<int:delivery_id>/unassign. Unrelated to place-merge; only the
    # estate-wide count moved.
    assert len(routes) == 566
    entry = next(r for r in routes if r["rule"] == "/api/v1/admin/place-groups/merge-preview")
    assert entry["methods"] == ["GET"]
    assert entry["endpoint"] == "admin.get_place_group_merge_preview"


# =========================================================================== #
# 12. REAL POSTGRES — precision, and the column the SQLite suite cannot see.
# =========================================================================== #

def _pg_world(pg_db):
    from business_app.models.customer_link import AddressGroup as AG
    return {
        "groups": AG.query.count(),
        "ledger": BottleLedger.query.count(),
        "balances": sum((Decimal(str(b.balance or 0)) for b in BottleBalance.query.all()),
                        Decimal("0.00")),
    }


def _pg_fixture(pg_db, phone_seed):
    """Two ungrouped customers with one address each, plus an admin, on PG."""
    def user(n, role, utype):
        u = User(email=f"pg{phone_seed}{n}@example.com", phone=f"+9989902{phone_seed}{n:03d}",
                 password_hash=hash_password("TestPassword123!"), first_name=f"P{n}",
                 last_name="G", user_type=utype, role=role, status=UserStatus.ACTIVE,
                 is_verified=True, created_at=datetime.now(UTC))
        pg_db.session.add(u)
        pg_db.session.commit()
        return u

    ua = user(1, UserRole.CUSTOMER, UserType.INDIVIDUAL)
    ub = user(2, UserRole.CUSTOMER, UserType.INDIVIDUAL)
    admin = user(3, UserRole.ADMIN, UserType.STAFF)
    addrs = []
    for u in (ua, ub):
        addr = UserAddress(user_id=u.id, title="Office", full_address="Office st",
                           latitude=LAT, longitude=LNG)
        pg_db.session.add(addr)
        pg_db.session.commit()
        addrs.append(addr)
    return ua, addrs[0], ub, addrs[1], admin


@pytest.mark.parametrize("stated", ["2.5", "12.005", "12.004"])
def test_a_fractional_resulting_balance_converges_on_REAL_POSTGRES(pg_app, pg_db, stated):
    """`stored_before` is quantized to cents but `resulting_balance` is NOT, so
    the delta can carry sub-cent precision into a Numeric(12,2) column.
    Postgres ROUNDS on store; SQLite does not, so a SQLite-only test proves
    nothing here. Whatever the column rounds to, the two figures must still
    agree — that is the guarantee, not the exact spelling."""
    ua, a, ub, b, admin = _pg_fixture(pg_db, 1)
    BottleTrackingService().admin_adjust_balance(
        user_id=ua.id, address_id=a.id, adjustment=Decimal("5"),
        actor_user_id=ua.id, notes="seed")
    pg_db.session.commit()

    client = pg_app.test_client()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(admin.id))}",
               "Content-Type": "application/json"}
    preview = client.get(f"{PREVIEW}?address_ids={a.id},{b.id}", headers=headers)
    assert preview.status_code == 200, preview.get_json()
    entry_ids = preview.get_json()["data"]["entry_ids"]

    created = client.post(CREATE, json={"addressIds": [a.id, b.id], "reason": "counted",
                                        "resultingBalance": stated,
                                        "previewEntryIds": entry_ids}, headers=headers)
    assert created.status_code == 201, created.get_json()
    group_id = created.get_json()["data"]["place_group_id"]

    pg_db.session.expire_all()
    scope = BottleScope.for_group(group_id)
    place = BottleTrackingService.get_place_balance(a.id)
    ledger = sum((Decimal(str(e.quantity)) for e in
                  BottleLedger.query.filter(*scope.ledger_filter()).all()), Decimal("0.00"))
    assert place == ledger, f"stated {stated}: place {place} != ledger {ledger}"
    # Numeric(12,2) rounds HALF AWAY FROM ZERO in Postgres, not half-to-even
    # like Python's default context — so 12.005 lands on 12.01, not 12.00. The
    # guarantee is that the two figures agree AND that the place holds the
    # column's rounding of the stated number, not Python's.
    assert place == Decimal(stated).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), (
        f"stated {stated} must land on its cent-rounded value, got {place}"
    )


def _pg_out_of_range_post(pg_db, phone_seed):
    """Commit a `resultingBalance` far wider than `Numeric(12, 2)` on Postgres."""
    ua, a, ub, b, admin = _pg_fixture(pg_db, phone_seed)
    BottleTrackingService().admin_adjust_balance(
        user_id=ua.id, address_id=a.id, adjustment=Decimal("5"),
        actor_user_id=ua.id, notes="seed")
    pg_db.session.commit()
    return ua, a, ub, b, admin


def _pg_post(pg_app, admin, body):
    client = pg_app.test_client()
    headers = {"Authorization": f"Bearer {create_access_token(identity=str(admin.id))}",
               "Content-Type": "application/json"}
    return client.post(CREATE, json=body, headers=headers)


def test_a_resulting_balance_wider_than_the_column_on_REAL_POSTGRES(pg_app, pg_db):
    """UPDATED — the outcome this pinned exactly (a 500) is now a 400.

    It used to assert that an out-of-range magnitude reached the
    `Numeric(12, 2)` column, died there as a Postgres `numeric field overflow`,
    and came back through the route's bare `except Exception` as a 500 for a
    plain client error (BUG 6). `_coerce_resulting_balance` now bounds the
    magnitude beside its finiteness check — the bound is read off the column
    itself — so the request is refused at the service and never reaches
    Postgres. The fix is exactly where the old docstring said it belonged, and
    NOT in a wider except arm at the route. Nothing is written either way,
    which is still asserted below.
    """
    ua, a, ub, b, admin = _pg_out_of_range_post(pg_db, 2)
    before = _pg_world(pg_db)

    resp = _pg_post(pg_app, admin, {"addressIds": [a.id, b.id], "reason": "counted",
                                    "resultingBalance": "1e400"})
    assert resp.status_code == 400, resp.get_json()
    pg_db.session.rollback()
    pg_db.session.expire_all()
    assert _pg_world(pg_db) == before, "the failed transaction must leave nothing behind"
    from business_app.models.customer_link import AddressGroup as AG
    assert AG.query.count() == before["groups"]
    assert UserAddress.query.get(a.id).address_group_id is None


def _pg_concurrent_double_join(pg_app, pg_db, phone_seed, monkeypatch=None):
    """Two reviewed joins of the SAME address C, run concurrently.

    With `monkeypatch`, a 2-party rendezvous is installed IMMEDIATELY AFTER
    `_assert_place_group_eligible` — i.e. after the membership fence has read
    `addresses.address_group_id` and before either transaction has committed
    anything. It changes no behaviour; it removes the timing luck from the
    window that decides the outcome, so the double-commit below is observed on
    every run rather than most of them. (If a fix ever locks the
    `user_addresses` rows BEFORE the fence, the second thread blocks there and
    never reaches the barrier; the barrier times out, is swallowed, and the
    first thread proceeds — the outcome is still observed, not hung.)

    Without it the two transactions interleave however the machine schedules
    them, which is the shape the convergence pin wants: no test-installed
    synchronisation anywhere near the code it is judging.
    """
    ua, a, ub, b, admin = _pg_fixture(pg_db, phone_seed)
    uc = User(email=f"pg{phone_seed}c@example.com", phone=f"+99899023{phone_seed}999",
              password_hash=hash_password("TestPassword123!"), first_name="C", last_name="G",
              user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER, status=UserStatus.ACTIVE,
              is_verified=True, created_at=datetime.now(UTC))
    pg_db.session.add(uc)
    pg_db.session.commit()
    c = UserAddress(user_id=uc.id, title="C", full_address="C st", latitude=LAT, longitude=LNG)
    pg_db.session.add(c)
    pg_db.session.commit()
    for user, addr, qty in ((ua, a, "6"), (ub, b, "5"), (uc, c, "4")):
        BottleTrackingService().admin_adjust_balance(
            user_id=user.id, address_id=addr.id, adjustment=Decimal(qty),
            actor_user_id=user.id, notes="seed")
    pg_db.session.commit()
    a_id, b_id, c_id, admin_id = a.id, b.id, c.id, admin.id
    c_seed_entry_id = (
        BottleLedger.query.filter_by(address_id=c_id).order_by(BottleLedger.id.asc()).one().id
    )

    if monkeypatch is not None:
        rendezvous = threading.Barrier(2, timeout=20)
        fence = CustomerLinkService._assert_place_group_eligible

        def fence_then_rendezvous(self, addresses):
            fence(self, addresses)
            try:
                rendezvous.wait()
            except threading.BrokenBarrierError:          # pragma: no cover - fixed code only
                pass

        monkeypatch.setattr(
            CustomerLinkService, "_assert_place_group_eligible", fence_then_rendezvous, raising=True
        )

    ready = threading.Barrier(2, timeout=30)
    results = {}

    def join(name, ids):
        with pg_app.app_context():
            from business_app import db as thread_db
            try:
                thread_db.session.execute(__import__("sqlalchemy").text("SET lock_timeout = '10s'"))
                ready.wait()
                CustomerLinkService().create_place_group(
                    ids, acting_admin_id=admin_id, reason=f"{name} counted 3",
                    resulting_balance=3,
                )
                results[name] = "ok"
            except Exception as exc:                       # noqa: BLE001
                thread_db.session.rollback()
                results[name] = type(exc).__name__
            finally:
                thread_db.session.remove()

    threads = [
        threading.Thread(target=join, args=("first", [a_id, c_id])),
        threading.Thread(target=join, args=("second", [b_id, c_id])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
        assert not t.is_alive(), "a concurrent reviewed join DEADLOCKED"

    pg_db.session.expire_all()
    memberships = {row.id: row.address_group_id for row in UserAddress.query.all()}
    return results, memberships, (a_id, b_id, c_id), c_seed_entry_id


def test_two_concurrent_reviewed_joins_of_the_same_address_on_REAL_POSTGRES(pg_app, pg_db):
    """UPDATED: every figure below changed when `_load_addresses` started locking.

    `with_for_update()` is a NO-OP on SQLite, so the whole lock argument is
    untested by the default suite — which is why this test exists at all.

    IT USED TO PIN THE DAMAGE: BOTH joins committed, and the loser's membership
    write was simply overwritten, so C's bottles were absorbed into one place
    while C ended up a member of the OTHER, and one place was left with a SINGLE
    member — below `create_place_group`'s own `PLACE_GROUP_MIN_ADDRESSES` fence.
    Every group still converged (`place balance == ledger sum`) and the bottles
    were conserved GLOBALLY, which is exactly why the previous spelling of this
    test — convergence plus "somebody grouped C" — passed while the attribution
    was being destroyed.

    NOW `_load_addresses` takes the joining `addresses` rows FOR NO KEY UPDATE,
    ascending by id, in ONE statement, BEFORE `_assert_place_group_eligible`
    reads them, and with `populate_existing()` so the refreshed columns are not
    discarded by the identity map. The loser blocks on C's row; when the winner
    commits, Postgres's EvalPlanQual re-check hands the loser the COMMITTED
    `address_group_id`, and the fence — now TRUE as a CONSEQUENCE of the lock
    rather than as a substitute for it — raises
    `PLACE_GROUP_ADDRESS_ALREADY_GROUPED`.

    The two joins touch DISJOINT other addresses ({A,C} and {B,C}), and both
    order their acquisition by id, so there is no cycle: the ascending-id rule is
    what makes that true in general rather than by luck.
    """
    results, memberships, (a_id, b_id, c_id), c_seed_entry_id = _pg_concurrent_double_join(
        pg_app, pg_db, 5
    )
    from business_app.models.customer_link import AddressGroup as AG

    assert set(results) == {"first", "second"}, f"a thread never reported: {results}"
    assert sorted(results.values()) == ["ValidationError", "ok"], (
        f"exactly one join of C may commit; got {results}"
    )
    assert memberships[c_id] is not None, f"nobody grouped C: {results}"

    # Exactly ONE place exists: the loser raised before flushing its group.
    groups = AG.query.all()
    assert len(groups) == 1, f"the losing join left an AddressGroup behind: {groups}"
    group = groups[0]
    assert memberships[c_id] == group.id

    # It converges, and it has TWO members — never the one-member shape
    # `PLACE_GROUP_MIN_ADDRESSES` forbids.
    members = sorted(addr for addr, gid in memberships.items() if gid == group.id)
    assert len(members) == 2, f"the surviving place has {len(members)} members"
    assert c_id in members
    scope = BottleScope.for_group(group.id)
    ledger = sum((Decimal(str(e.quantity)) for e in
                  BottleLedger.query.filter(*scope.ledger_filter()).all()), Decimal("0.00"))
    assert BottleTrackingService.get_place_balance(members[0]) == ledger == Decimal("3.00")

    # C's own history is in the place C actually belongs to — the attribution
    # assertion the old convergence-only spelling could not make.
    _refresh()
    c_seed = BottleLedger.query.get(c_seed_entry_id)
    assert c_seed.address_group_id == memberships[c_id], (
        "C's own ledger row is stranded in a place C is not a member of"
    )
    assert BottleLedger.query.filter_by(
        address_group_id=memberships[c_id], address_id=c_id
    ).count() >= 1

    # The loser's other address kept its own scope untouched.
    loser_address = b_id if a_id in members else a_id
    assert memberships[loser_address] is None
    assert sorted(str(Decimal(str(row.balance))) for row in BottleBalance.query.all()) == \
        ["3.00", "5.00"] or sorted(
            str(Decimal(str(row.balance))) for row in BottleBalance.query.all()
        ) == ["3.00", "6.00"], "the loser's own place must be untouched"


def test_two_concurrent_joins_of_the_same_address_must_not_both_commit(
    pg_app, pg_db, monkeypatch
):
    """FIXED — the xfail is gone. THIS IS THE FALSE ARGUMENT, closed.

    WAS: `_assert_place_group_eligible` read `addr.address_group_id` from an
    UNLOCKED select (`_load_addresses` had no `with_for_update`), so under READ
    COMMITTED both transactions saw the address ungrouped, both passed the
    fence, both absorbed its balance and re-scoped its ledger rows, and the
    second commit simply overwrote `addresses.address_group_id`. The address's
    history stayed with the FIRST place while its membership pointed at the
    SECOND, and the loser place was left with one member.

    Both `_absorb_joiners_into_group` and `_split_bottles_out_of_place` rested
    their ABBA-freedom argument on "of two concurrent transactions on one
    address exactly one passes its fence". THAT CLAIM IS FALSE FOR TWO JOINS,
    and it was false for a structural reason: under READ COMMITTED both read the
    same pre-image, so a READ-BASED test can never serialise anything. The
    docstrings have been REPLACED, not repaired — deadlock-freedom now rests on
    ORDERING ALONE.

    The fence survives as a CORRECTNESS check and is now TRUE as a CONSEQUENCE of
    the `addresses` row lock: the loser blocks, and EvalPlanQual hands it the
    winner's COMMITTED pointer when it wakes. That last step needs
    `populate_existing()` on the locking load — without it SQLAlchemy re-reads
    the row and DISCARDS the columns, and the fence evaluates the pre-image
    again, so the fix would ship as a no-op that every SQLite test still passes.
    This test is the break-test for that.

    NOTE the rendezvous this harness installs is now UNREACHABLE by the second
    thread: it sits after the fence, and the loser blocks BEFORE the fence, on
    rung 1. The barrier times out, is swallowed, and the first thread proceeds —
    the outcome is still observed, not hung. That the interleave can no longer be
    constructed is itself the evidence.
    """
    results, memberships, (a_id, b_id, c_id), c_seed_entry_id = _pg_concurrent_double_join(
        pg_app, pg_db, 6, monkeypatch=monkeypatch
    )
    assert list(results.values()).count("ok") == 1, (
        f"exactly one join of C may commit; got {results}"
    )
