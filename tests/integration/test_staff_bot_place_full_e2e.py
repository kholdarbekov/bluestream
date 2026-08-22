"""Staff-bot PLACE surfaces, end to end against the REAL backend.

WHAT MAKES THIS FILE DIFFERENT FROM ``tests/unit/test_staff_bot_place_surfaces.py``
AND ``tests/unit/test_staff_bot_over_returned.py``.

Those two feed hand-built dicts to the bot's formatters. They are pinned against
the real payload shape by one contract test each, but every *value* they render
is a literal. This module removes the literals: every payload the handlers see
here is produced by a REAL service write (``record_bottles_delivered``,
``record_bottles_returned``, ``record_standalone_collection``,
``admin_adjust_balance``, ``issue_fine``, the place-lifecycle service) and then
fetched back through the REAL HTTP routes the bot calls in production, over a
REAL driver JWT. ``staff_bot.api_client`` is replaced by :class:`_Bridge`, which
re-implements the response-unwrapping contract of ``StaffAPIClient._make_request``
on top of Flask's test client — so a backend rename, a serializer regression or a
route-level 400 shows up as a broken driver screen here, not as a silently green
fixture.

The axis: statement body + total, the actionable-places filter, the address
picker, fines, the quantity picker/guard, the over-returned state on both the
single-place and the multi-place path, the at-door return prompt's three arms,
and ``place_bottle_balance_signed`` surviving the ``current_delivery`` whitelist.

CONSERVATION IS ALWAYS ASSERTED AS A PAIR. ``_pair(address_id)`` returns
``(stored balance, ledger sum)`` for a place. ``_create_ledger_entry`` moves both
by the same quantity, so any write that moves only one is invisible to a
single-sided assertion and can only ever be repaired by the §7.4 merge review.

SEVEN scenarios turned out to describe REAL DEFECTS. Each was marked
``xfail(strict=True)`` with the intended contract asserted, so the suite stayed
green while the bug stayed named and the xfail flipped to a failure the moment it
was fixed. One mapped scenario (bot-level double-tap) turned out NOT to be
reachable as stated — see ``test_double_tapping_save_without_note_cannot_double_post``,
which pins why, and its sibling that showed the same double-debit WAS reachable
through the api_client's retry. That route is now closed for POST: the client
re-sends only ``RETRY_SAFE_METHODS`` ({GET, HEAD, PUT}) after an ambiguous
failure, so a collection or fine POST is never auto-replayed. What remains is a
DRIVER-mediated repeat, which the per-intent idempotency token dedupes.

The non-finite fine amount was TWO defects, both now FIXED. The BACKEND half
(``test_the_fine_route_rejects_a_non_finite_fine_amount``) is closed by
``BottleTrackingService._as_decimal``, the SSOT coercion every bottle write
funnels through — that route has no serializer at all, so the admin routes'
``allow_inf_nan=False`` does nothing for it and a per-route fix would have left
it open. The original xfail reason claimed both NaN and Infinity end in a 500;
they do not. ``Decimal('NaN') <= 0`` raises ``InvalidOperation`` (500), but
``Decimal('Infinity') <= 0`` is simply False, so an INFINITE fine WAS accepted,
committed, and read back to the next driver as "Active fines: 1 (inf Uzs)". The
BOT half (``test_fine_amount_rejects_nan_and_infinity``) was a distinct defect in
``receive_fine_amount``'s own ``float(text)`` + ``<= 0`` check and is closed by
``_parse_positive_amount``.

The staff-bot flow cluster is likewise closed: the flow dict now has an owner
(clear-on-entry in ``_begin_flow``, clear-in-finally in ``_finalize_collection``
/ ``receive_fine_note``), every at-door handler re-anchors on the ``delivery_id``
in the callback instead of reading ``current_delivery`` blind, and a retried
delivery PUT is rendered as the idempotent success it is.

The retry cluster is CLOSED TOO (2026-08-03). ``record_standalone_collection``
and ``issue_fine`` now accept a per-INTENT client token, the bot mints one at the
confirm step and re-sends it on every attempt of that intent, the stored key is
composed server-side as ``{namespace}:client:{actor_user_id}:{token}`` so a
driver cannot poison a natural key, and the driver-session tally moved INSIDE
``_create_ledger_entry``'s dedup fence so a deduped write no longer bumps
``bottles_collected_from_customers``. The three ``xfail(strict=True)`` markers
that named those defects are gone; the tests below now assert the fixed world.
"""

import asyncio
import importlib.util
import pathlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from flask_jwt_extended import create_access_token
from sqlalchemy import func

from business_app import db as _db
from business_app.models.bottle import BottleBalance, BottleFine, BottleLedger
from business_app.models.delivery import Delivery, DeliveryPerson
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductCategory
from business_app.models.user import User, UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
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
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.delivery import DeliveryKeyboards

# The Update/context harness is shared with the unit module on purpose: one
# definition of "an update that satisfies @require_auth + @require_delivery_driver".
from tests.unit.test_staff_bot_place_surfaces import (  # noqa: E402
    _callbacks,
    _edited_markup,
    _edited_text,
    _make_update_context,
    _patch_handler,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


# ===========================================================================
# Harness — a real HTTP bridge standing in for staff_bot.api_client
# ===========================================================================


class _Resp:
    """The subset of ``staff_bot.api_client.APIResponse`` the handlers read."""

    def __init__(self, success, data=None, error=None, status_code=None, error_code=None):
        self.success = success
        self.data = data
        self.error = error
        self.status_code = status_code
        self.error_code = error_code


class _Bridge:
    """Async-context stand-in for ``api_client`` that speaks to the real app.

    Mirrors ``StaffAPIClient._make_request``'s contract: 200/201 unwrap
    ``payload['data']`` (absent key ⇒ ``None``), anything else is a failure
    carrying the backend's ``error_code``. Every call is recorded, so a test can
    assert the exact POST body the driver's tap produced.
    """

    def __init__(self, http, token):
        self.http = http
        self.token = token
        self.calls = []
        self.fail_next_collection = 0
        # Path suffixes whose FIRST response is lost. ``StaffAPIClient`` retries a
        # connect-phase failure on any verb and an ambiguous-phase failure on
        # GET/HEAD/PUT only (staff_bot/api_client.py — ``RETRY_SAFE_METHODS``), so a
        # duplicate POST is no longer something the real client emits. ``_Bridge``
        # keeps the hard-coded double send deliberately: what these tests pin is the
        # SERVER-side idempotency fence, which must hold against a duplicate from
        # any client, proxy or replay.
        self.retry_suffixes = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    # -- plumbing ---------------------------------------------------------

    def _request(self, method, path, payload=None):
        response = self._send(method, path, payload)
        if any(path.endswith(suffix) for suffix in self.retry_suffixes):
            response = self._send(method, path, payload)
        return response

    def _send(self, method, path, payload=None):
        self.calls.append({"method": method, "path": path, "payload": payload})
        response = self.http.open(
            path,
            method=method,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        body = response.get_json(silent=True) or {}
        if response.status_code in (200, 201):
            return _Resp(True, body.get("data"), status_code=response.status_code)
        details = body.get("details") or {}
        return _Resp(
            False,
            None,
            error=body.get("message") or body.get("error") or "error",
            status_code=response.status_code,
            error_code=body.get("error_code") or details.get("error_code"),
        )

    def posted(self, suffix):
        """Every recorded call whose path ends with ``suffix``."""
        return [c for c in self.calls if c["path"].endswith(suffix)]

    # -- the endpoints the bottle/at-door handlers use ---------------------

    async def get_customer_bottle_summary(self, token, customer_id):
        return self._request("GET", f"/api/v1/staff/bottles/customer/{customer_id}/summary")

    async def get_customer_bottle_addresses(self, token, customer_id):
        return self._request("GET", f"/api/v1/staff/bottles/customer/{customer_id}/addresses")

    async def record_bottle_collection(self, token, data):
        # A network round trip is a real suspension point: without one,
        # asyncio.gather would run two taps strictly sequentially and could
        # never model the concurrency this simulates.
        await asyncio.sleep(0)
        if self.fail_next_collection > 0:
            self.fail_next_collection -= 1
            self.calls.append(
                {"method": "POST", "path": "/api/v1/staff/bottles/collection",
                 "payload": data, "simulated_failure": True}
            )
            return _Resp(False, None, error="boom", status_code=500, error_code=None)
        return self._request("POST", "/api/v1/staff/bottles/collection", data)

    async def create_bottle_fine(self, token, data):
        return self._request("POST", "/api/v1/staff/bottles/fine", data)

    async def get_active_deliveries(self, token):
        return self._request("GET", "/api/v1/staff/delivery/active")

    async def update_delivery_status(self, token, delivery_id, status, metadata=None):
        payload = {"status": status}
        if metadata:
            payload["metadata"] = metadata
        return self._request("PUT", f"/api/v1/staff/delivery/{delivery_id}/status", payload)


@pytest.fixture(autouse=True)
def _no_redis_flow_state(monkeypatch):
    """``flow_state`` mirrors the flow flag into Redis; irrelevant here."""
    from staff_bot.utils import flow_state

    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())


@pytest.fixture
def http(app):
    """A FRESH test client per test.

    The session-scoped ``client`` fixture leaks JWT cookies between tests, which
    is exactly what the 403/401 role assertions below must not inherit.
    """
    return app.test_client()


@pytest.fixture
def i18n_spy(monkeypatch):
    """Echo each key back and record its kwargs.

    Without this, ``staff_bot/i18n.py`` humanises a missing key's last segment
    and DROPS every interpolation kwarg, so an assertion on rendered copy would
    silently depend on the translation seed having run.
    """
    calls = []

    def fake_get(key, language=None, *args, **kwargs):
        calls.append({"key": key, "language": language, "kwargs": kwargs})
        return key

    monkeypatch.setattr(i18n, "get", fake_get)
    return calls


def _kwargs_for(calls, key):
    return next(c["kwargs"] for c in calls if c["key"] == key)


# ===========================================================================
# Harness — real data builders (no hand-built BottleBalance rows)
# ===========================================================================


_SEQ = {"n": 0}


def _next():
    _SEQ["n"] += 1
    return _SEQ["n"]


def _customer(db, *, first="Cust", last="Omer"):
    n = _next()
    user = User(
        email=f"place-c{n}@example.com",
        phone=f"+9989011{n:05d}",
        password_hash=hash_password("TestPassword123!"),
        first_name=first,
        last_name=last,
        user_type=UserType.INDIVIDUAL,
        role=UserRole.CUSTOMER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _staff(db, role, *, driver_profile=False, profile_active=True):
    n = _next()
    user = User(
        email=f"place-s{n}@example.com",
        phone=f"+9989022{n:05d}",
        password_hash=hash_password("TestPassword123!"),
        first_name="Staff",
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
                full_name="Staff Driver",
                phone=user.phone,
                email=user.email,
                is_active=profile_active,
                is_available=True,
            )
        )
        db.session.commit()
    return user


def _address(db, user, *, title="Home", full_address=None, lat=41.3111, lng=69.2797):
    address = UserAddress(
        user_id=user.id,
        title=title,
        full_address=full_address or f"{_next()} Test St, Tashkent",
        street_address="1 Test St",
        city="Tashkent",
        latitude=lat,
        longitude=lng,
    )
    db.session.add(address)
    db.session.commit()
    return address


def _group(db, addresses, admin, *, label=None, **review):
    """A real place group through the real admin service path."""
    group = CustomerLinkService().create_place_group(
        [a.id for a in addresses], acting_admin_id=admin.id, reason="coworkers",
        label=label, **review,
    )
    db.session.expire_all()
    return group


def _token(app, user):
    with app.app_context():
        return create_access_token(identity=str(user.id))


# -- real bottle movements ---------------------------------------------------


def _deliver(db, user, address, quantity, *, order_id=None):
    """A real DELIVERY through ``record_bottles_delivered`` (+quantity)."""
    BottleTrackingService().record_bottles_delivered(
        order_id=order_id if order_id is not None else 900000 + _next(),
        user_id=user.id,
        address_id=address.id,
        quantity=Decimal(str(quantity)),
    )
    db.session.commit()


def _return_at(db, user, address, quantity, *, order_id=None):
    """A real RETURN_ON_DELIVERY through ``record_bottles_returned`` (-quantity)."""
    BottleTrackingService().record_bottles_returned(
        user.id, address.id, Decimal(str(quantity)),
        order_id=order_id if order_id is not None else 910000 + _next(),
        delivery_id=_next(),
    )
    db.session.commit()


def _collect(db, user, address, quantity, actor):
    """A real STANDALONE_COLLECTION through the service (-quantity)."""
    BottleTrackingService().record_standalone_collection(
        user_id=user.id, address_id=address.id,
        quantity=Decimal(str(quantity)), actor_user_id=actor.id,
    )
    db.session.commit()


def _adjust(db, user, address, delta, actor, notes="fixture adjustment"):
    BottleTrackingService().admin_adjust_balance(
        user.id, address.id, Decimal(str(delta)), actor_user_id=actor.id, notes=notes
    )
    db.session.commit()


def _pair(address_id):
    """``(stored balance, ledger sum)`` for the PLACE this address belongs to.

    The two figures a coupled write moves together and a decoupled write does
    not. Conservation assertions must read BOTH.
    """
    scope = BottleTrackingService.resolve_scope(address_id)
    stored = BottleTrackingService.get_place_balance(address_id)
    ledger = (
        _db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
        .filter(*scope.ledger_filter())
        .scalar()
    )
    return Decimal(str(stored or 0)), Decimal(str(ledger or 0))


def _drift_place_to(db, address, stored):
    """Reproduce the dev-DB "address 24" shape: a stored figure with NO ledger.

    THE BALANCE ROW ITSELF IS THE SUBJECT here, and it is still created by the
    real writer (``set_initial_balance``); only the ledger rows are then removed,
    which is precisely what a pre-ledger manual adjustment looks like on
    production data. This is the one shape that cannot be produced by any
    balance-coupled write, and it is the reason
    ``_create_ledger_backfill_entry`` exists.
    """
    admin = _staff(db, UserRole.ADMIN)
    BottleTrackingService().set_initial_balance(
        address.user_id, address.id, Decimal(str(stored)), actor_user_id=admin.id
    )
    db.session.commit()
    scope = BottleTrackingService.resolve_scope(address.id)
    for row in BottleLedger.query.filter(*scope.ledger_filter()).all():
        db.session.delete(row)
    db.session.commit()
    assert _pair(address.id) == (Decimal(str(stored)), Decimal("0.00"))


# -- handler runners ---------------------------------------------------------


def _bottle_handler(monkeypatch, bridge, *, language="en"):
    from staff_bot.handlers.delivery import bottle_collection as mod

    handler = BottleCollectionHandler()
    _patch_handler(monkeypatch, handler, mod, bridge)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value=language))
    return handler


def _status_handler(monkeypatch, bridge, *, language="en"):
    from staff_bot.handlers.delivery import status_update as mod
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    handler = StatusUpdateHandler()
    _patch_handler(monkeypatch, handler, mod, bridge)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value=language))
    return handler


def _active_handler(monkeypatch, bridge, *, language="en"):
    from staff_bot.handlers.delivery import active_delivery as mod
    from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler

    handler = ActiveDeliveryHandler()
    _patch_handler(monkeypatch, handler, mod, bridge)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value=language))
    return handler


def _show_statement(monkeypatch, bridge, customer_id, *, language="en"):
    """Drive the whole statement screen: real /summary + real /addresses."""
    handler = _bottle_handler(monkeypatch, bridge, language=language)
    update, context = _make_update_context(callback_data=f"staff_bottle_customer_{customer_id}")
    asyncio.run(handler.show_customer_bottle_statement(update, context))
    return update, context, handler


def _summary(http, token, customer_id):
    """The REAL /summary payload, as the bot receives it."""
    resp = http.get(
        f"/api/v1/staff/bottles/customer/{customer_id}/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _address_rows(http, token, customer_id):
    resp = http.get(
        f"/api/v1/staff/bottles/customer/{customer_id}/addresses",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["data"]


def _body_lines(text):
    return [line for line in text.splitlines() if line.startswith("•")]


def _total_line(text):
    return next(line for line in text.splitlines() if "otal" in line and ":" in line)


# -- delivery builders (for the at-door arm) ---------------------------------


def _bottle_product(db, *, per_unit="1", tracks=True):
    category = ProductCategory(name=f"Water {_next()}", description="w", is_active=True)
    db.session.add(category)
    db.session.commit()
    product = Product(
        name=f"Pure Water {_next()}", description="d", category_id=category.id,
        size="19L", volume=19.0, volume_unit="L", base_price=Decimal("15000.00"),
        stock_quantity=100, min_stock_level=1, max_stock_level=500, is_active=True,
        tracks_returnable_bottles=tracks,
        returnable_bottles_per_unit=Decimal(str(per_unit)) if tracks else Decimal("0"),
    )
    db.session.add(product)
    db.session.commit()
    return product


def _delivery_for(db, customer, address, driver, *, product, quantity=1):
    """A CARD-paid, already-settled order ARRIVED at the door.

    CARD + COMPLETED keeps the cash-collection engine entirely out of this
    axis: the at-door bottle prompt is what is under test, not COD.
    """
    order = Order(
        user_id=customer.id, order_number=f"ORD-PLACE-{_next()}",
        status=OrderStatus.OUT_FOR_DELIVERY,
        subtotal=Decimal("15000.00"), delivery_fee=Decimal("0.00"),
        discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
        total_amount=Decimal("15000.00"), payment_method=PaymentMethod.CARD,
        delivery_address_id=address.id,
    )
    db.session.add(order)
    db.session.flush()
    db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=quantity,
                             unit_price=Decimal("15000.00"),
                             total_price=Decimal("15000.00") * quantity))
    db.session.add(Payment(order_id=order.id, user_id=customer.id,
                           payment_method=PaymentMethod.CARD, amount=order.total_amount,
                           amount_collected=order.total_amount,
                           outstanding_amount=Decimal("0.00"), currency="UZS",
                           status=PaymentStatus.COMPLETED, payment_id=f"card_{_next()}"))
    delivery = Delivery(order_id=order.id, delivery_person_id=driver.id,
                        status=DeliveryStatus.ARRIVED,
                        scheduled_date=datetime.now(UTC),
                        scheduled_time_slot="09:00-12:00")
    db.session.add(delivery)
    db.session.commit()
    return order, delivery


def _open_delivery_card(monkeypatch, bridge, delivery_id):
    """Drive ``view_active_delivery`` over the REAL /delivery/active payload."""
    handler = _active_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_view_active_{delivery_id}")
    asyncio.run(handler.view_active_delivery(update, context))
    return context


def _reach_bottle_prompt(monkeypatch, bridge, context, delivery_id):
    """Non-cash DELIVERED edge → the bottle prompt (status_update.py:334)."""
    handler = _status_handler(monkeypatch, bridge)
    update, _ = _make_update_context(
        callback_data=f"staff_execute_status_{delivery_id}_delivered"
    )
    asyncio.run(handler.execute_status_change(update, context))
    return update, handler


# ===========================================================================
# 1. Statement header total — cluster_scopes[].balance, never addresses[]
# ===========================================================================


def test_statement_total_comes_from_cluster_scopes_and_body_from_place_balance(
    app, db, http
):
    """The Group-9 shape, rebuilt with real writes: +6 / +6 / −4 ⇒ ONE place at 8.

    ``cluster_scopes`` rows are keyed ``balance``; ``addresses`` rows are keyed
    ``place_balance``. Reading the wrong key on either side renders without
    raising — a silent 0 header, or a body that counts one place twice.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="work"), _address(db, u2, title="work")
    # u1 owns TWO addresses at the one place, which is what makes the two
    # sources numerically DIFFERENT: `cluster_scopes` has one row (8) while
    # `addresses` has two (8 + 8). Without the second owned address both
    # spellings sum to the same number and this test could not tell them apart.
    annexe = _address(db, u1, title="work-annexe")
    _group(db, [a1, a2, annexe], admin)

    _deliver(db, u1, a1, 6)
    _deliver(db, u2, a2, 6)
    _return_at(db, u2, a2, 4)
    assert _pair(a1.id) == (Decimal("8.00"), Decimal("8.00"))
    assert _pair(annexe.id) == (Decimal("8.00"), Decimal("8.00"))

    summary = _summary(http, _token(app, driver), u1.id)
    # Derived, not hand-copied: the two spellings must both be present and agree.
    assert [s["balance"] for s in summary["cluster_scopes"]] == [8.0]
    assert [a["place_balance"] for a in summary["addresses"]] == [8.0, 8.0]
    # Summing the BODY's key is the double count the header must never do.
    assert sum(a["place_balance"] for a in summary["addresses"]) == 16.0
    # ...and the two key names are disjoint, so reading the wrong one on either
    # side yields nothing at all rather than a plausible-looking number.
    assert all("balance" not in a for a in summary["addresses"])
    assert all("place_balance" not in scope for scope in summary["cluster_scopes"])

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert _total_line(text).endswith(": 8")
    assert all(": 8" in line for line in _body_lines(text))
    assert len(_body_lines(text)) == 1


def test_statement_total_is_zero_when_cluster_scopes_is_absent(app, db, http):
    """A serializer regression that drops ``cluster_scopes`` takes the header to
    0 while the body still lists the place — no exception anywhere.

    Pinning the divergence keeps the failure mode DETECTABLE. It is also the
    reason nobody may "fix" the header by summing ``addresses``: that would
    double-count every customer owning two addresses at one place.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="work"), _address(db, u2, title="work")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 7)

    summary = _summary(http, _token(app, driver), u1.id)
    del summary["cluster_scopes"]

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert _total_line(text).endswith(": 0")
    assert any(": 7" in line for line in _body_lines(text))


def test_statement_total_negative_renders_over_returned_copy(app, db, http, i18n_spy):
    """A real over-collection: deliver +2, collect 5 ⇒ the place is at −3.

    The header is a SIGNED sum. A bare "-3" on a driver's screen reads as a bug
    at the door, so the same "over-returned by N" copy the body uses must fire,
    with the MAGNITUDE.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 2)
    _collect(db, u, a, 5, driver)
    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))

    summary = _summary(http, _token(app, driver), u.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert "staff.delivery.place_over_returned" in text
    assert "-3" not in text
    assert all(c["kwargs"] == {"count": "3"}
               for c in i18n_spy if c["key"] == "staff.delivery.place_over_returned")


def test_statement_total_mixed_signs_nets_to_zero_but_body_lists_both_places(
    app, db, http, i18n_spy
):
    """Two real places, +4 and −4. The header nets to 0 — and 0 is NOT the
    over-returned arm — while the body must still list both actionable doors."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    plus, minus = _address(db, u, title="plus"), _address(db, u, title="minus")
    _deliver(db, u, plus, 4)
    _deliver(db, u, minus, 1)
    _collect(db, u, minus, 5, driver)
    assert _pair(plus.id) == (Decimal("4.00"), Decimal("4.00"))
    assert _pair(minus.id) == (Decimal("-4.00"), Decimal("-4.00"))

    summary = _summary(http, _token(app, driver), u.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert _total_line(text).endswith(": 0")
    assert len(_body_lines(text)) == 2
    assert any(": 4" in line for line in _body_lines(text))
    assert any("staff.delivery.place_over_returned" in line for line in _body_lines(text))
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "4"}
    assert "staff.delivery.no_bottle_balance" not in text


def test_statement_total_sums_as_decimal_without_binary_float_noise(app, db, http):
    """1.1 + 2.2 is 3.3000000000000003 in binary floating point. The values come
    from two real ``admin_adjust_balance`` writes, so this cannot pass on a
    fixture that happens to hold clean numbers."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    one, two = _address(db, u, title="one"), _address(db, u, title="two")
    _adjust(db, u, one, "1.1", admin)
    _adjust(db, u, two, "2.2", admin)

    summary = _summary(http, _token(app, driver), u.id)
    assert sorted(s["balance"] for s in summary["cluster_scopes"]) == [1.1, 2.2]

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert _total_line(text).endswith(": 3.3")


# ===========================================================================
# 2. Statement body — one line per DISTINCT place
# ===========================================================================


def test_statement_body_omits_zero_places_and_keeps_nonzero_ones(app, db, http):
    """+5, a place driven back to exactly 0 by a matching collection, and −2."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    pos, zero, neg = (_address(db, u, title="pos"), _address(db, u, title="zero"),
                      _address(db, u, title="neg"))
    _deliver(db, u, pos, 5)
    _deliver(db, u, zero, 3)
    _collect(db, u, zero, 3, driver)
    _deliver(db, u, neg, 1)
    _collect(db, u, neg, 3, driver)
    assert [_pair(a.id) for a in (pos, zero, neg)] == [
        (Decimal("5.00"), Decimal("5.00")),
        (Decimal("0.00"), Decimal("0.00")),
        (Decimal("-2.00"), Decimal("-2.00")),
    ]

    summary = _summary(http, _token(app, driver), u.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert len(_body_lines(text)) == 2
    assert not any("zero" in line for line in _body_lines(text))
    # Naming what SURVIVED, not just what vanished: a filter that dropped the
    # negative place instead of the zero one would still leave two lines.
    assert any(line.startswith("• pos") and ": 5" in line for line in _body_lines(text))
    assert any(line.startswith("• neg") and "Place over returned" in line
               for line in _body_lines(text))
    assert not any("-2" in line for line in _body_lines(text))


def test_statement_body_empty_state_keys_off_what_was_rendered(app, db, http):
    """A customer whose every place has SETTLED used to get a header and nothing
    else, because the empty state keyed off "owns no addresses"."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a1, a2 = _address(db, u, title="one"), _address(db, u, title="two")
    for addr in (a1, a2):
        _deliver(db, u, addr, 2)
        _collect(db, u, addr, 2, driver)
    assert [_pair(a.id) for a in (a1, a2)] == [
        (Decimal("0.00"), Decimal("0.00")), (Decimal("0.00"), Decimal("0.00"))
    ]

    summary = _summary(http, _token(app, driver), u.id)
    # The rows exist — this is NOT the "no addresses" case.
    assert len(summary["addresses"]) == 2
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert "No bottle balance" in text
    assert _body_lines(text) == []
    assert not text.endswith("\n")
    assert "" not in text.splitlines()[1:]


def test_statement_body_collapses_two_owned_addresses_in_one_place_to_one_line(
    app, db, http
):
    """``_place_key`` dedupes on ``('g', address_group_id)``. If ``/summary``
    ever stops emitting that key the fallback is ``('a', address_id)`` and the
    same 7 bottles print twice — read by the driver as 14."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    mine_a = _address(db, u1, title="work")
    mine_b = _address(db, u1, title="work-annexe")
    theirs = _address(db, u2, title="work")
    _group(db, [mine_a, mine_b, theirs], admin)
    _deliver(db, u1, mine_a, 7)
    assert _pair(mine_b.id) == (Decimal("7.00"), Decimal("7.00"))

    summary = _summary(http, _token(app, driver), u1.id)
    assert len(summary["addresses"]) == 2          # one row per OWNED address
    assert {a["place_balance"] for a in summary["addresses"]} == {7.0}

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert len(_body_lines(text)) == 1
    assert _total_line(text).endswith(": 7")


def test_statement_body_never_collapses_two_ungrouped_addresses(app, db, http):
    """Two genuinely different doors. An over-eager dedupe keyed on anything but
    the address id would hide one of them from the driver."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    home, dacha = _address(db, u, title="home"), _address(db, u, title="dacha")
    _deliver(db, u, home, 3)
    _deliver(db, u, dacha, 4)

    summary = _summary(http, _token(app, driver), u.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    assert len(_body_lines(text)) == 2
    assert _total_line(text).endswith(": 7")


def test_statement_body_marks_only_grouped_places_with_the_shared_marker(app, db, http):
    """``is_grouped`` on ``/summary`` and ``address_group_id is not None`` on
    ``/addresses`` must agree, or a private door is announced as shared."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    shared_mine = _address(db, u1, title="office")
    shared_theirs = _address(db, u2, title="office")
    solo = _address(db, u1, title="home")
    _group(db, [shared_mine, shared_theirs], admin)
    _deliver(db, u1, shared_mine, 5)
    _deliver(db, u1, solo, 2)

    summary = _summary(http, _token(app, driver), u1.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")

    office = next(line for line in _body_lines(text) if "office" in line)
    home = next(line for line in _body_lines(text) if "home" in line)
    assert "👥" in office
    assert "👥" not in home

    # ...and the picker's own marker, computed from the OTHER endpoint, agrees.
    rows = {r["address_id"]: r for r in _address_rows(http, _token(app, driver), u1.id)}
    assert rows[shared_mine.id]["is_grouped"] is True
    assert rows[solo.id]["is_grouped"] is False


def test_statement_body_and_picker_do_not_round_a_fractional_negative_to_zero(
    app, db, http, i18n_spy
):
    """A place at −0.5 survives the ``!= 0`` actionable filter. ``int()`` would
    announce it as "over-returned by 0" and label its button "(↩0)";
    ``format_quantity`` is the only thing preventing both."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u, title="half")
    _deliver(db, u, a, "0.5")
    _collect(db, u, a, 1, driver)
    assert _pair(a.id) == (Decimal("-0.50"), Decimal("-0.50"))

    token = _token(app, driver)
    summary = _summary(http, token, u.id)
    BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "0.5"}

    rows = _address_rows(http, token, u.id)
    label = DeliveryKeyboards.bottle_address_selection("en", u.id, rows).inline_keyboard[0][0].text
    assert "↩0.5" in label
    assert "↩0)" not in label


def test_statement_body_escapes_html_in_the_address_title(app, db, http):
    """``parse_mode='HTML'`` — a customer-controlled title must not inject markup."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u, title="<b>Ofis</b> & Co")
    _deliver(db, u, a, 3)

    summary = _summary(http, _token(app, driver), u.id)
    assert summary["addresses"][0]["address_title"] == "<b>Ofis</b> & Co"

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert "&lt;b&gt;Ofis&lt;/b&gt; &amp; Co" in text
    assert "<b>Ofis</b>" not in text


def test_statement_and_picker_agree_on_the_full_address_fallback(app, db, http):
    """Title absent ⇒ both surfaces fall back to the first 30 chars of
    ``full_address``. The slice is duplicated in bottle_collection.py and
    keyboards/delivery.py; if one drifts the driver sees two names for one door."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    long_address = "12 Very Long Boulevard Name, Yunusobod, Tashkent, Uzbekistan"
    a = _address(db, u, title=None, full_address=long_address)
    _deliver(db, u, a, 3)

    token = _token(app, driver)
    summary = _summary(http, token, u.id)
    rows = _address_rows(http, token, u.id)
    expected = long_address[:30]

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    label = DeliveryKeyboards.bottle_address_selection("en", u.id, rows).inline_keyboard[0][0].text

    assert expected in text
    assert expected in label


# ===========================================================================
# 3. Statement header — fines are per ACCOUNT while balances are per PLACE
# ===========================================================================


def test_statement_fines_line_counts_only_active_fines_and_formats_currency(
    app, db, http
):
    """Two live fines + one waived. ``active_fines_count`` filters
    PENDING+INVOICED; a waived fine leaking in inflates the driver's screen."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 5)
    token = _token(app, driver)

    for _ in range(3):
        resp = http.post(
            "/api/v1/staff/bottles/fine",
            json={"customer_id": u.id, "address_id": a.id, "quantity": 1,
                  "fine_amount": 50000, "notes": "missing"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()
    third = BottleFine.query.order_by(BottleFine.id.desc()).first()
    BottleTrackingService().waive_fine(third.id, driver.id, notes="goodwill")
    db.session.commit()

    summary = _summary(http, token, u.id)
    assert summary["active_fines_count"] == 2
    assert summary["total_fine_amount"] == 100000.0

    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    fine_line = next(line for line in text.splitlines() if line.startswith("⚠️"))
    assert ": 2 " in fine_line
    assert "100,000" in fine_line


def test_statement_shows_no_fine_line_at_all_when_none_are_active(app, db, http):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    text = BottleCollectionHandler._format_bottle_statement(
        _summary(http, _token(app, driver), u.id), "en"
    )
    assert not any(line.startswith("⚠️") for line in text.splitlines())


def test_statement_fine_counters_are_per_account_while_balances_are_per_place(
    app, db, http
):
    """PINNING AN ASYMMETRY, not endorsing it. The header mixes two scopes: the
    balance is the whole PLACE's (including the coworker's empties) while
    ``active_fines_count`` is filtered on ``BottleFine.user_id``. A fine issued
    against the coworker a minute ago is invisible on the screen the driver is
    standing in front of. If either scope is ever changed, this test is the only
    thing that names the intended shape."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 5)

    token = _token(app, driver)
    resp = http.post(
        "/api/v1/staff/bottles/fine",
        json={"customer_id": u2.id, "address_id": a2.id, "quantity": 2, "fine_amount": 40000},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_json()

    mine = _summary(http, token, u1.id)
    assert mine["addresses"][0]["place_balance"] == 5.0     # the whole PLACE
    assert mine["active_fines_count"] == 0                  # ...but not their fine
    theirs = _summary(http, token, u2.id)
    assert theirs["active_fines_count"] == 1
    assert theirs["addresses"][0]["place_balance"] == 5.0


# ===========================================================================
# 4. The picker — sourced from /addresses, filtered on != 0
# ===========================================================================


def test_the_two_endpoints_disagree_on_row_count_by_design(app, db, http):
    """D7 at the source. ``/summary`` returns one row per OWNED address while
    ``/addresses`` returns one per PLACE, keyed to the lowest-id owned member —
    so a picker built from ``/summary`` offers an address the quantity-cap
    lookup can never match, and the driver dead-ends on a place that
    demonstrably has empties."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    mine_a = _address(db, u1, title="work")
    mine_b = _address(db, u1, title="work-annexe")
    theirs = _address(db, u2, title="work")
    _group(db, [mine_a, mine_b, theirs], admin)
    _deliver(db, u1, mine_a, 6)

    token = _token(app, driver)
    summary = _summary(http, token, u1.id)
    rows = _address_rows(http, token, u1.id)

    assert len(summary["addresses"]) == 2
    assert len(rows) == 1
    assert rows[0]["address_id"] == min(mine_a.id, mine_b.id)
    # Deduping either shape yields the SAME single place...
    assert len(BottleCollectionHandler._actionable_places(summary)) == 1
    assert len(BottleCollectionHandler._actionable_places({"addresses": rows})) == 1
    # ...but only /addresses guarantees the representative id the cap uses.
    assert {r["address_id"] for r in
            BottleCollectionHandler._actionable_places({"addresses": rows})} == {rows[0]["address_id"]}


def test_picker_offers_exactly_one_row_for_a_two_address_place(app, db, http, monkeypatch):
    """The runnable half of D7: drive the handler end to end and assert that the
    single offered button carries the SAME address id the cap lookup will use."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    mine_a = _address(db, u1, title="work")
    mine_b = _address(db, u1, title="work-annexe")
    theirs = _address(db, u2, title="work")
    _group(db, [mine_a, mine_b, theirs], admin)
    _deliver(db, u1, mine_a, 6)

    token = _token(app, driver)
    bridge = _Bridge(http, token)
    update, context, _ = _show_statement(monkeypatch, bridge, u1.id)

    representative = min(mine_a.id, mine_b.id)
    offered = _callbacks(_edited_markup(update))
    # Single actionable place ⇒ the picker is skipped and the ACTION keyboard
    # is shown, still keyed to the representative address.
    assert f"staff_bottle_collect_{u1.id}_{representative}" in offered
    assert not any(c.endswith(f"_{max(mine_a.id, mine_b.id)}") for c in offered)
    flow = context.user_data["pending_bottle_collection_flow"]
    assert flow["address_id"] == representative
    assert flow["picker_place_balances"] == {representative: 6.0}


def test_a_failed_addresses_call_shows_an_error_screen_not_an_empty_picker(
    app, db, http, monkeypatch
):
    """"The call failed" and "nothing is actionable" are different screens.
    Swallowing a 401/500/timeout into ``or []`` prints a balance above a bare
    Back button — the exact unexplained dead end this handler exists to remove."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 4)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    reported = AsyncMock()
    monkeypatch.setattr(handler, "_handle_api_response_error", reported)

    failing = _Resp(False, None, error="upstream timeout", status_code=504)

    async def _boom(token, customer_id):
        return failing

    monkeypatch.setattr(bridge, "get_customer_bottle_addresses", _boom)

    update, context = _make_update_context(callback_data=f"staff_bottle_customer_{u.id}")
    asyncio.run(handler.show_customer_bottle_statement(update, context))

    assert reported.await_count == 1
    assert reported.await_args.args[1] is failing
    update.callback_query.edit_message_text.assert_not_called()


def test_a_successful_but_empty_addresses_call_shows_the_statement_with_only_back(
    app, db, http, monkeypatch
):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 2)
    _collect(db, u, a, 2, driver)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    reported = AsyncMock()
    monkeypatch.setattr(handler, "_handle_api_response_error", reported)

    update, context = _make_update_context(callback_data=f"staff_bottle_customer_{u.id}")
    asyncio.run(handler.show_customer_bottle_statement(update, context))

    reported.assert_not_awaited()
    callbacks = _callbacks(_edited_markup(update))
    assert not any(c.startswith("staff_bottle_collect_") for c in callbacks)
    assert not any(c.startswith("staff_bottle_fine_") for c in callbacks)
    assert "No bottle balance" in _edited_text(update)


def test_single_actionable_positive_place_auto_skips_the_picker(app, db, http, monkeypatch):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 4)

    bridge = _Bridge(http, _token(app, driver))
    update, context, _ = _show_statement(monkeypatch, bridge, u.id)

    callbacks = _callbacks(_edited_markup(update))
    assert f"staff_bottle_collect_{u.id}_{a.id}" in callbacks
    assert f"staff_bottle_fine_{u.id}_{a.id}" in callbacks
    assert not any(c.startswith("staff_bottle_addr_") for c in callbacks)
    assert context.user_data["pending_bottle_collection_flow"]["address_id"] == a.id


def test_single_actionable_negative_place_auto_skips_with_collect_hidden(
    app, db, http, monkeypatch
):
    """The single-place half of the over-returned rule. A ``> 0`` vs ``!= 0``
    slip re-offers Collect and dead-ends the driver on the quantity guard."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)
    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))

    bridge = _Bridge(http, _token(app, driver))
    update, _, _ = _show_statement(monkeypatch, bridge, u.id)

    callbacks = _callbacks(_edited_markup(update))
    assert f"staff_bottle_fine_{u.id}_{a.id}" in callbacks
    assert f"staff_bottle_collect_{u.id}_{a.id}" not in callbacks
    assert "staff_back_to_main" in callbacks


def test_multi_place_picker_hides_collect_only_for_the_negative_place(
    app, db, http, monkeypatch
):
    """``picker_place_balances`` is keyed by the address id parsed as an int from
    the callback data. A str/int key mismatch falls back to "fail open" and
    silently re-offers Collect at the over-returned door."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    big, neg, small = (_address(db, u, title="big"), _address(db, u, title="neg"),
                       _address(db, u, title="small"))
    _deliver(db, u, big, 5)
    _deliver(db, u, neg, 1)
    _collect(db, u, neg, 3, driver)
    _deliver(db, u, small, 1)

    bridge = _Bridge(http, _token(app, driver))
    update, context, handler = _show_statement(monkeypatch, bridge, u.id)

    offered = _callbacks(_edited_markup(update))
    for addr in (big, neg, small):
        assert f"staff_bottle_addr_{u.id}_{addr.id}" in offered

    def _select(addr):
        sub, _ = _make_update_context(callback_data=f"staff_bottle_addr_{u.id}_{addr.id}")
        asyncio.run(handler.select_address(sub, context))
        return _callbacks(_edited_markup(sub))

    negative = _select(neg)
    assert f"staff_bottle_fine_{u.id}_{neg.id}" in negative
    assert f"staff_bottle_collect_{u.id}_{neg.id}" not in negative
    for addr in (big, small):
        chosen = _select(addr)
        assert f"staff_bottle_collect_{u.id}_{addr.id}" in chosen
        assert f"staff_bottle_fine_{u.id}_{addr.id}" in chosen


def test_select_address_fails_open_when_the_picker_balance_map_is_missing(
    app, db, http, monkeypatch
):
    """Cleared ``user_data`` (bot restart) + a still-tappable button. Hiding
    Collect on a place that really has empties is the worse failure, and
    ``start_collection`` re-reads the live balance anyway."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_bottle_addr_{u.id}_{a.id}")
    context.user_data.pop("pending_bottle_collection_flow", None)

    asyncio.run(handler.select_address(update, context))

    assert f"staff_bottle_collect_{u.id}_{a.id}" in _callbacks(_edited_markup(update))


def test_actionable_filter_keeps_negative_places_and_drops_the_zero_one(app, db, http):
    """The rows come from the real ``/addresses`` payload: +3, 0.00, −2, −0.5."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    pos, zero, neg, frac = (_address(db, u, title="pos"), _address(db, u, title="zero"),
                            _address(db, u, title="neg"), _address(db, u, title="frac"))
    _deliver(db, u, pos, 3)
    _deliver(db, u, zero, 2)
    _collect(db, u, zero, 2, driver)
    _deliver(db, u, neg, 1)
    _collect(db, u, neg, 3, driver)
    _deliver(db, u, frac, "0.5")
    _collect(db, u, frac, 1, driver)

    rows = _address_rows(http, _token(app, driver), u.id)
    by_id = {r["address_id"]: r["place_balance"] for r in rows}
    assert by_id == {pos.id: 3.0, zero.id: 0.0, neg.id: -2.0, frac.id: -0.5}

    survivors = BottleCollectionHandler._actionable_places({"addresses": rows})
    assert {r["address_id"] for r in survivors} == {pos.id, neg.id, frac.id}


def test_actionable_filter_skips_rows_with_a_null_address_id(app, db, http):
    """``bot.py`` routes ``^staff_bottle_addr_\\d+_\\d+$``. A ``None`` id builds
    a button no handler can ever answer — a silent dead tap."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 3)

    rows = _address_rows(http, _token(app, driver), u.id)
    # Defensive shape, derived from a real row so the key set cannot go stale.
    orphan = dict(rows[0], address_id=None, place_balance=5.0)

    survivors = BottleCollectionHandler._actionable_places({"addresses": rows + [orphan]})
    assert [r["address_id"] for r in survivors] == [a.id]

    # The keyboard does NOT defend itself — it renders whatever it is handed —
    # so the filter above is the only thing standing between a null id and a
    # button no handler can answer. Asserting the unguarded shape is what keeps
    # the assertion below from being true by construction.
    unguarded = DeliveryKeyboards.bottle_address_selection("en", u.id, rows + [orphan])
    assert f"staff_bottle_addr_{u.id}_None" in _callbacks(unguarded)

    markup = DeliveryKeyboards.bottle_address_selection("en", u.id, survivors)
    assert f"staff_bottle_addr_{u.id}_None" not in _callbacks(markup)
    assert f"staff_bottle_addr_{u.id}_{a.id}" in _callbacks(markup)


def test_addresses_route_maps_a_place_to_the_lowest_id_owned_address(app, db, http):
    """``get_customer_place_rows`` maps each place back through
    ``own_group_addresses.setdefault`` under an id-ascending query. Losing the
    ``order_by`` makes the representative address — and therefore every
    downstream cap/fine key — nondeterministic."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    first = _address(db, u1, title="work")
    second = _address(db, u1, title="work-annexe")
    coworker = _address(db, u2, title="work")
    group = _group(db, [first, second, coworker], admin)
    _deliver(db, u1, first, 2)
    _deliver(db, u1, second, 3)
    _deliver(db, u2, coworker, 4)

    rows = _address_rows(http, _token(app, driver), u1.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["address_id"] == min(first.id, second.id)
    assert row["place_balance"] == 9.0          # includes the coworker's 4
    assert row["is_grouped"] is True
    assert row["place_group_id"] == group.id
    assert "bottle_balance_id" not in row


# ===========================================================================
# 5. Quantity picker + guard
# ===========================================================================


@pytest.mark.parametrize(
    "balance,expected_numbers,expected_all",
    [
        (1, [1], 1),
        (5, [1, 2, 3, 4, 5], 5),
        (6, [1, 2, 3, 4, 5, 6], 6),
        (10, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 10),
        (11, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 11),
    ],
)
def test_quantity_picker_cap_boundaries(
    app, db, http, monkeypatch, balance, expected_numbers, expected_all
):
    """1 / 5 / 6 / 10 / 11 — the min/max off-by-ones that either offer a quantity
    the place does not hold or hide the last collectable bottle. Each balance is
    reached by a real delivery and read back through ``start_collection``."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, balance)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(
        callback_data=f"staff_bottle_collect_{u.id}_{a.id}"
    )
    asyncio.run(handler.start_collection(update, context))

    callbacks = _callbacks(_edited_markup(update))
    prefix = f"staff_bottle_qty_{u.id}_{a.id}_"
    offered = [int(c[len(prefix):]) for c in callbacks if c.startswith(prefix)]
    assert sorted(set(offered)) == sorted(set(expected_numbers + [expected_all]))
    assert max(offered) == expected_all
    assert "staff_flow_cancel" in callbacks
    assert context.user_data["pending_bottle_collection_flow"]["balance"] == balance


def test_quantity_picker_never_offers_more_than_the_place_balance(
    app, db, http, monkeypatch
):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 7)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))

    prefix = f"staff_bottle_qty_{u.id}_{a.id}_"
    offered = [int(c[len(prefix):]) for c in _callbacks(_edited_markup(update))
               if c.startswith(prefix)]
    # EXACT, not a bound: 7 sits above the 1-5 row and below the 10 cap, so a
    # picker that offered 1-5 plus "All (7)" and silently dropped the 6/7 row
    # would still satisfy `max == 7`.
    assert sorted(set(offered)) == [1, 2, 3, 4, 5, 6, 7]
    assert max(offered) == 7


def test_quantity_guard_splits_over_returned_from_empty_and_keeps_both_actionable(
    app, db, http, monkeypatch, i18n_spy
):
    """Both arms must be branched BEFORE ``bottle_collection_qty_picker``, whose
    ``max(0, int(balance))`` clamp renders an explanation-free keyboard."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    neg, zero = _address(db, u, title="neg"), _address(db, u, title="zero")
    _deliver(db, u, neg, 1)
    _collect(db, u, neg, 4, driver)
    _deliver(db, u, zero, 2)
    _collect(db, u, zero, 2, driver)
    assert _pair(neg.id) == (Decimal("-3.00"), Decimal("-3.00"))
    assert _pair(zero.id) == (Decimal("0.00"), Decimal("0.00"))

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)

    def _tap(addr):
        update, context = _make_update_context(
            callback_data=f"staff_bottle_collect_{u.id}_{addr.id}"
        )
        asyncio.run(handler.start_collection(update, context))
        return update

    negative = _tap(neg)
    assert _edited_text(negative) == "staff.delivery.place_over_returned"
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "3"}
    neg_callbacks = _callbacks(_edited_markup(negative))
    assert f"staff_bottle_fine_{u.id}_{neg.id}" in neg_callbacks
    assert f"staff_bottle_collect_{u.id}_{neg.id}" not in neg_callbacks
    assert not any(c.startswith("staff_bottle_qty_") for c in neg_callbacks)

    empty = _tap(zero)
    assert _edited_text(empty) == "staff.delivery.no_bottle_balance"
    zero_callbacks = _callbacks(_edited_markup(empty))
    assert f"staff_bottle_fine_{u.id}_{zero.id}" in zero_callbacks
    assert not any(c.startswith("staff_bottle_qty_") for c in zero_callbacks)


def test_quantity_guard_treats_a_positive_fractional_place_as_the_empty_arm(
    app, db, http, monkeypatch
):
    """DOCUMENTED ASYMMETRY. The picker labelled this place "(0.5)" a moment ago,
    but ``int(0.5) == 0`` so the guard takes the empty arm. It must still keep
    the actions on screen — never a dead end — which is the whole point of the
    branch. Pinned so nobody "fixes" it into a picker with no buttons."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, "0.5")

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))

    assert "No bottle balance" in _edited_text(update)
    callbacks = _callbacks(_edited_markup(update))
    assert f"staff_bottle_fine_{u.id}_{a.id}" in callbacks
    assert f"staff_bottle_collect_{u.id}_{a.id}" not in callbacks
    assert not any(c.startswith("staff_bottle_qty_") for c in callbacks)


def test_start_collection_re_reads_the_live_place_balance(app, db, http, monkeypatch):
    """The statement was rendered at +6; an admin then drove the place to −1.
    Caching the balance in the flow to save a round trip would let the driver
    collect six bottles that are no longer there."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 6)

    bridge = _Bridge(http, _token(app, driver))
    _, context, handler = _show_statement(monkeypatch, bridge, u.id)
    assert context.user_data["pending_bottle_collection_flow"]["picker_place_balances"] == {a.id: 6.0}

    _adjust(db, u, a, -7, admin, notes="stock correction")
    assert _pair(a.id) == (Decimal("-1.00"), Decimal("-1.00"))

    update, _ = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))

    assert "Place over returned" in _edited_text(update)
    assert not any(c.startswith("staff_bottle_qty_") for c in _callbacks(_edited_markup(update)))


def test_start_collection_falls_back_to_zero_for_an_address_absent_from_addresses(
    app, db, http, monkeypatch
):
    """A place that was split away and settled to 0 no longer appears in
    ``/addresses``. The ``for``/``break`` lookup must leave ``place_balance`` at
    its initialiser — removing either turns a routine split into an
    UnboundLocal/stale-value bug at the door."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 5)
    CustomerLinkService().remove_address_from_group(
        a1.id, acting_admin_id=admin.id, reason="moved out"
    )
    _db.session.expire_all()

    token = _token(app, driver)
    assert _address_rows(http, token, u1.id) == []      # nothing actionable left

    bridge = _Bridge(http, token)
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u1.id}_{a1.id}")
    asyncio.run(handler.start_collection(update, context))

    assert "No bottle balance" in _edited_text(update)
    assert not any(c.startswith("staff_bottle_qty_") for c in _callbacks(_edited_markup(update)))


def test_qty_callback_is_ignored_when_the_flow_is_not_a_collect_flow(
    app, db, http, monkeypatch
):
    """Without the ``action`` guard a stale qty tap arms the collect note step
    inside a FINE flow, and the driver's next typed text finalises a collection
    they never chose.

    The tap is REFUSED, not processed — that is what this test protects. It used
    to also assert the refusal was SILENT (`edit_message_text.assert_not_called`),
    which pinned a second defect: a bare `answer()` stops the spinner and tells
    the driver nothing, which is indistinguishable from a crashed bot, so they
    tap harder. Since 2026-08-22 the refusal says so and takes the dead buttons
    away (`BottleCollectionHandler._refuse_stale_tap`). Both halves are asserted
    below: the fine flow is untouched, AND the driver is told.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(
        callback_data=f"staff_bottle_qty_{u.id}_{a.id}_3"
    )
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": u.id, "address_id": a.id, "action": "fine", "fine_quantity": 2,
    }

    asyncio.run(handler.pick_collection_qty(update, context))

    # The load-bearing half: the FINE flow is not armed for a collection note.
    flow = context.user_data["pending_bottle_collection_flow"]
    assert "quantity" not in flow
    assert flow["action"] == "fine", "the driver's actual flow must survive the refusal"
    assert flow["fine_quantity"] == 2

    # The other half: the driver is told, rather than left tapping a dead button.
    update.callback_query.answer.assert_awaited_once()
    assert update.callback_query.answer.await_args.kwargs.get("text"), (
        "a refusal with no text is indistinguishable from a crashed bot"
    )
    update.callback_query.edit_message_text.assert_called_once()


# ===========================================================================
# 6. Collection — payload, real route effects, receipts
# ===========================================================================


def _run_full_collection(monkeypatch, bridge, customer_id, address_id, qty, note):
    """statement → Collect → pick qty → typed note, all through the handlers."""
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(
        callback_data=f"staff_bottle_collect_{customer_id}_{address_id}"
    )
    asyncio.run(handler.start_collection(update, context))

    pick, _ = _make_update_context(
        callback_data=f"staff_bottle_qty_{customer_id}_{address_id}_{qty}"
    )
    asyncio.run(handler.pick_collection_qty(pick, context))

    note_update, _ = _make_update_context(message_text=note)
    asyncio.run(handler.receive_collection_note(note_update, context))
    return note_update, context, handler


def test_collection_post_body_is_the_four_route_keys_plus_the_retry_token(
    app, db, http, monkeypatch
):
    """WAS ``…_is_exactly_the_four_route_keys``. The body gained a FIFTH key on
    2026-08-03: the per-intent idempotency token minted in ``pick_collection_qty``.

    Still a whole-body assertion — a sixth key, or a renamed one, still fails —
    but the token's VALUE is random, so it is asserted by shape
    (``uuid4().hex``) rather than by equality. Hard-coding it would pin the mint
    implementation instead of the wire contract.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    _run_full_collection(monkeypatch, bridge, u.id, a.id, 3, "left at reception")

    posts = bridge.posted("/bottles/collection")
    assert len(posts) == 1
    payload = posts[0]["payload"]
    assert set(payload) == {
        "customer_id", "address_id", "quantity", "notes", "idempotency_key",
    }
    assert payload["customer_id"] == u.id
    assert payload["address_id"] == a.id
    assert payload["quantity"] == 3
    assert payload["notes"] == "left at reception"
    # The backend validates with `\A[A-Za-z0-9_-]{8,64}\Z` + `fullmatch`
    # (bottle_tracking_service.CLIENT_IDEMPOTENCY_TOKEN_PATTERN); a mint that
    # drifted outside it would 400 every collection with
    # BOTTLE_IDEMPOTENCY_KEY_INVALID.
    assert re.fullmatch(r"[0-9a-f]{32}", payload["idempotency_key"])


def test_real_route_collection_debits_the_place_and_writes_a_coupled_ledger_entry(
    app, db, http, monkeypatch
):
    """CONSERVATION PAIR. ``_create_ledger_entry`` moves the stored balance AND
    the ledger sum by the same quantity; any path that touches only one creates
    drift only the §7.4 merge review can ever close."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    group = _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 3)
    _deliver(db, u2, a2, 4)
    assert _pair(a1.id) == (Decimal("7.00"), Decimal("7.00"))

    bridge = _Bridge(http, _token(app, driver))
    _run_full_collection(monkeypatch, bridge, u1.id, a1.id, 4, "took four")
    _db.session.expire_all()

    assert _pair(a1.id) == (Decimal("3.00"), Decimal("3.00"))
    entry = (BottleLedger.query
             .filter_by(event_type=BottleLedgerEventType.STANDALONE_COLLECTION)
             .order_by(BottleLedger.id.desc()).first())
    assert entry.quantity == Decimal("-4.00")
    assert entry.address_group_id == group.id
    assert entry.balance_after == Decimal("3.00")
    assert entry.user_id == u1.id
    assert entry.actor_user_id == str(driver.id) or int(entry.actor_user_id) == driver.id


def test_collection_at_a_grouped_address_can_take_a_coworkers_empties(
    app, db, http, monkeypatch
):
    """The whole point of the re-key: all +5 arrived at member B's door and the
    driver is standing at member A's. Any residual ``(user_id, address_id)``
    check would refuse exactly the scenario places exist for."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    group = _group(db, [a1, a2], admin)
    _deliver(db, u2, a2, 5)

    bridge = _Bridge(http, _token(app, driver))
    _run_full_collection(monkeypatch, bridge, u1.id, a1.id, 5, "")
    _db.session.expire_all()

    assert _pair(a1.id) == (Decimal("0.00"), Decimal("0.00"))
    assert _pair(a2.id) == (Decimal("0.00"), Decimal("0.00"))
    entry = (BottleLedger.query
             .filter_by(event_type=BottleLedgerEventType.STANDALONE_COLLECTION)
             .one())
    assert entry.user_id == u1.id
    assert entry.address_id == a1.id
    # SCOPE ATTRIBUTION, not just the quantity: a debit stamped to a1's OWN
    # scope instead of the group's conserves bottles globally while stranding
    # them at a place the driver was never standing at. The pair above cannot
    # see that; this can.
    assert entry.address_group_id == group.id
    assert entry.quantity == Decimal("-5.00")
    assert entry.balance_after == Decimal("0.00")
    assert BottleBalance.query.filter_by(address_id=a1.id,
                                         address_group_id=None).count() == 0


def test_collection_receipt_names_an_over_returned_remainder(
    app, db, http, monkeypatch, i18n_spy
):
    """``remaining_balance`` is deliberately NOT clamped by the route, so the
    sign branch in ``_finalize_collection`` is the only thing stopping a raw
    "-3" reaching the driver."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 2)

    bridge = _Bridge(http, _token(app, driver))
    # The picker caps at 2, so a 5 can only be reached by a stale/hand-made
    # callback — which is exactly the shape this receipt exists for.
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": u.id, "address_id": a.id, "action": "collect", "quantity": 5,
    }
    note_update, _ = _make_update_context(message_text="took the lot")
    asyncio.run(handler.receive_collection_note(note_update, context))
    _db.session.expire_all()

    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))
    assert _kwargs_for(
        i18n_spy, "staff.delivery.bottle_collection_recorded_over_returned"
    ) == {"quantity": 5, "remaining": "3"}
    assert not any(c["key"] == "staff.delivery.bottle_collection_recorded" for c in i18n_spy)


def test_collection_receipt_positive_remainder_uses_the_normal_key(
    app, db, http, monkeypatch, i18n_spy
):
    """``staff_bot/i18n.py`` SWALLOWS ``str.format`` errors and prints the raw
    template, so a kwarg slip ships a template string to the driver rather than
    raising."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 6)

    bridge = _Bridge(http, _token(app, driver))
    _run_full_collection(monkeypatch, bridge, u.id, a.id, 4, "note")

    assert _kwargs_for(i18n_spy, "staff.delivery.bottle_collection_recorded") == {
        "quantity": 4, "remaining": "2",
    }


def test_collection_via_save_without_note_posts_an_empty_string(app, db, http, monkeypatch):
    """``notes`` must be ``''`` (not None, not missing), and the receipt must be
    edited into the SAME message — ``_say`` picks its channel from
    ``update.callback_query`` vs ``update.message``, and getting that wrong
    raises AttributeError inside a SUCCESS path, after the bottles were booked."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 4)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))
    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{a.id}_2")
    asyncio.run(handler.pick_collection_qty(pick, context))

    save, _ = _make_update_context(callback_data="staff_bottle_collect_save_no_note")
    asyncio.run(handler.save_collection_no_note(save, context))

    payload = bridge.posted("/bottles/collection")[0]["payload"]
    assert payload["notes"] == ""
    assert payload["quantity"] == 2
    # The receipt landed in the SAME message via the callback branch of ``_say``.
    # ``update.message`` is None on a callback update, so a ``_say`` that reached
    # for ``reply_text`` would have raised AttributeError inside a SUCCESS path,
    # after the bottles were already booked. Asserting the AWAIT COUNT (not the
    # harness's own ``save.message is None``) is what makes this a claim about
    # production: exactly one edit, and it carries the receipt.
    assert save.callback_query.edit_message_text.await_count == 1
    assert "Bottle collection recorded" in _edited_text(save)
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("2.00"), Decimal("2.00"))


def test_collection_flow_bails_cleanly_when_it_lost_customer_address_or_quantity(
    app, db, http, monkeypatch
):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 4)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(message_text="a note nobody asked for")
    context.user_data["pending_bottle_collection_flow"] = {}

    asyncio.run(handler.receive_collection_note(update, context))

    assert bridge.posted("/bottles/collection") == []
    assert update.message.reply_text.call_args.args[0] == "Error occurred"
    assert "pending_bottle_collection_flow" not in context.user_data
    assert _pair(a.id) == (Decimal("4.00"), Decimal("4.00"))


def test_a_failed_collection_post_must_not_leave_the_flow_armed(
    app, db, http, monkeypatch
):
    """FIXED — the xfail is gone.

    WAS: ``_finalize_collection`` cleared the flow only on SUCCESS, so after a
    500 it still carried ``action='collect'`` + ``quantity`` and the global text
    router (staff_bot/bot.py) finalised a collection for ANY subsequent text —
    the driver's next message silently re-posted a collection nobody confirmed.

    NOW the flow is cleared in a ``finally``: success, refusal, backend failure
    or crash all end the flow. A collection that did not land costs the driver
    one re-pick, never a phantom debit at the customer's door.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    bridge.fail_next_collection = 1
    handler = _bottle_handler(monkeypatch, bridge)
    monkeypatch.setattr(handler, "_handle_api_response_error", AsyncMock())

    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))
    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{a.id}_5")
    asyncio.run(handler.pick_collection_qty(pick, context))

    first, _ = _make_update_context(message_text="left at reception")
    asyncio.run(handler.receive_collection_note(first, context))

    # The driver types anything at all afterwards. Nothing may be re-posted.
    second, _ = _make_update_context(message_text="ok")
    asyncio.run(handler.receive_collection_note(second, context))

    real_posts = [c for c in bridge.posted("/bottles/collection")
                  if not c.get("simulated_failure")]
    assert real_posts == []
    assert (context.user_data.get("pending_bottle_collection_flow")
            or {}).get("quantity") is None
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))


def test_double_tapping_save_without_note_cannot_double_post_through_the_bot(
    app, db, http, monkeypatch
):
    """THE MAPPED SCENARIO IS NOT REACHABLE THROUGH THE BOT, and this pins why.

    ``Application.builder()`` is built without ``concurrent_updates``
    (staff_bot/bot.py:195-201), so PTB processes the two taps SEQUENTIALLY, and
    ``_finalize_collection`` clears the flow on success — the second tap finds an
    empty flow and bails before posting. The double-debit was real, but it
    entered through the api_client's retry, not through the driver's thumb; see
    ``test_a_duplicate_collection_post_dedupes_on_the_drivers_intent_token``,
    which pins the server-side fence that now closes it.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    monkeypatch.setattr(handler, "_handle_api_response_error", AsyncMock())
    update, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(update, context))
    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{a.id}_5")
    asyncio.run(handler.pick_collection_qty(pick, context))

    for _ in range(2):
        tap, _ = _make_update_context(callback_data="staff_bottle_collect_save_no_note")
        asyncio.run(handler.save_collection_no_note(tap, context))

    _db.session.expire_all()
    assert len(bridge.posted("/bottles/collection")) == 1
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))


def test_a_duplicate_collection_post_dedupes_on_the_drivers_intent_token(app, db, http):
    """FIXED — the xfail is gone. The token identifies the DECISION, not the
    transmission, so two deliveries of one POST body are one collection.

    WAS: ``record_standalone_collection`` was the ONLY bottle write path with no
    idempotency key (``delivery:<order>``, ``return:<order>:<delivery>`` and
    ``fine_paid:<id>`` all had one), so a backend that committed and then lost
    the response got the identical body again and double-debited the place.

    This is a SERVER-SIDE fence and it is deliberately tested here at the HTTP
    boundary rather than through the bot: after 2026-08-03 ``StaffAPIClient``
    itself no longer re-POSTs an ambiguous failure (``RETRY_SAFE_METHODS`` is
    GET/HEAD/PUT), but a duplicate can still arrive from a proxy, a replay, or
    any future client. A driver who genuinely collects twice goes through the
    picker twice and mints a second token — which is why the key is per-intent
    and not a hash of the body.

    Asserting only the balance would also be satisfied by a fix that CLAMPED the
    second debit instead of recognising it as the same event, so the stored key
    is asserted outright.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    body = {"customer_id": u.id, "address_id": a.id, "quantity": 5, "notes": "retry",
            "idempotency_key": "b7f1c93e2b0447a18e2d6c5f0a19d3e4"}
    headers = {"Authorization": f"Bearer {_token(app, driver)}"}
    for _ in range(2):
        assert http.post("/api/v1/staff/bottles/collection", json=body,
                         headers=headers).status_code == 200
    _db.session.expire_all()

    entries = (BottleLedger.query
               .filter_by(event_type=BottleLedgerEventType.STANDALONE_COLLECTION)
               .all())
    assert len(entries) == 1
    # The stored key is COMPOSED server-side, so the driver cannot poison a
    # natural key such as `delivery:{order_id}` — `uq_bottle_ledger_idempotency`
    # is unique on the key ALONE, with no scope predicate.
    assert entries[0].idempotency_key == (
        f"collect:client:{driver.id}:b7f1c93e2b0447a18e2d6c5f0a19d3e4"
    )
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))


def test_the_same_token_at_a_different_place_is_refused_not_silently_swallowed(
    app, db, http
):
    """A dedup hit on the key ALONE is not proof of a replay.

    Without the post-fetch comparison (``_assert_replay_matches_collection``) a
    driver could reuse one token at every door: HTTP 200, no ledger row, no
    balance move and — since the tally moved inside the dedup fence — no session
    discrepancy either, so he keeps the bottles and the conservation oracle sees
    a consistent world.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    a_user = _customer(db)
    a_addr = _address(db, a_user)
    _deliver(db, a_user, a_addr, 5)
    b_user = _customer(db)
    b_addr = _address(db, b_user)
    _deliver(db, b_user, b_addr, 8)
    headers = {"Authorization": f"Bearer {_token(app, driver)}"}
    tok = "b7f1c93e2b0447a18e2d6c5f0a19d3e4"

    assert http.post("/api/v1/staff/bottles/collection", headers=headers, json={
        "customer_id": a_user.id, "address_id": a_addr.id, "quantity": 5,
        "idempotency_key": tok}).status_code == 200
    resp = http.post("/api/v1/staff/bottles/collection", headers=headers, json={
        "customer_id": b_user.id, "address_id": b_addr.id, "quantity": 8,
        "idempotency_key": tok})

    assert resp.status_code == 409
    assert (resp.get_json() or {}).get("error_code") == "BOTTLE_IDEMPOTENCY_KEY_REUSED"
    _db.session.expire_all()
    assert _pair(b_addr.id) == (Decimal("8.00"), Decimal("8.00"))   # untouched
    assert _pair(a_addr.id) == (Decimal("0.00"), Decimal("0.00"))   # the real one landed


def test_collection_is_refused_at_an_address_the_customer_does_not_belong_to(
    app, db, http
):
    """``_assert_user_in_scope`` replaced the old ``balance.user_id`` check. Skip
    it and a hand-crafted callback books a collection onto a stranger's place."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    victim, stranger = _customer(db), _customer(db)
    theirs = _address(db, victim)
    _deliver(db, victim, theirs, 5)
    ledger_before = BottleLedger.query.count()

    resp = http.post(
        "/api/v1/staff/bottles/collection",
        json={"customer_id": stranger.id, "address_id": theirs.id, "quantity": 2},
        headers={"Authorization": f"Bearer {_token(app, driver)}"},
    )

    assert resp.status_code == 400
    assert resp.get_json().get("error_code") == "BOTTLE_SCOPE_MEMBERSHIP_REQUIRED"
    _db.session.expire_all()
    assert BottleLedger.query.count() == ledger_before
    assert _pair(theirs.id) == (Decimal("5.00"), Decimal("5.00"))


def test_bottle_routes_reject_customers_and_deactivated_drivers(app, db, http):
    """``require_staff_roles('delivery_driver','operator')`` + the
    ``DeliveryPerson.is_active`` fence are applied PER ROUTE, so a new route
    added without them is invisible until someone tests it."""
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    plain = _customer(db)
    operator = _staff(db, UserRole.OPERATOR)
    dead_driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True,
                         profile_active=False)

    reads = [f"/api/v1/staff/bottles/customer/{u.id}/summary",
             f"/api/v1/staff/bottles/customer/{u.id}/addresses"]

    def _hdr(user):
        return {"Authorization": f"Bearer {_token(app, user)}"}

    for path in reads:
        assert http.get(path, headers=_hdr(plain)).status_code == 403
        assert http.get(path, headers=_hdr(operator)).status_code == 200
        blocked = http.get(path, headers=_hdr(dead_driver))
        assert blocked.status_code == 403
        assert blocked.get_json().get("error_code") == "STAFF_ACCOUNT_DEACTIVATED"

    collection = {"customer_id": u.id, "address_id": a.id, "quantity": 1}
    fine = {"customer_id": u.id, "address_id": a.id, "quantity": 1, "fine_amount": 1000}
    assert http.post("/api/v1/staff/bottles/collection", json=collection,
                     headers=_hdr(plain)).status_code == 403
    assert http.post("/api/v1/staff/bottles/fine", json=fine,
                     headers=_hdr(plain)).status_code == 403
    dead = http.post("/api/v1/staff/bottles/collection", json=collection,
                     headers=_hdr(dead_driver))
    assert dead.status_code == 403
    assert dead.get_json().get("error_code") == "STAFF_ACCOUNT_DEACTIVATED"
    # The operator is allowed on the WRITES too.
    assert http.post("/api/v1/staff/bottles/collection", json=collection,
                     headers=_hdr(operator)).status_code == 200
    assert http.post("/api/v1/staff/bottles/fine", json=fine,
                     headers=_hdr(operator)).status_code == 200


def test_collection_quantity_zero_negative_and_string_are_handled_at_the_boundary(
    app, db, http
):
    """The route's truthiness guard and the service's sign guard cover DIFFERENT
    holes; drop either and a negative quantity becomes a bottle-minting write."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)
    headers = {"Authorization": f"Bearer {_token(app, driver)}"}

    def _post(qty):
        return http.post("/api/v1/staff/bottles/collection",
                         json={"customer_id": u.id, "address_id": a.id, "quantity": qty},
                         headers=headers)

    zero = _post(0)
    assert zero.status_code == 400
    assert "required" in (zero.get_json().get("message") or "").lower()

    negative = _post(-3)
    assert negative.status_code == 400
    assert "positive" in (negative.get_json().get("message") or "").lower()

    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))

    assert _post("3").status_code == 200
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("2.00"), Decimal("2.00"))


def test_collection_tallies_the_drivers_open_session_and_no_ops_without_one(
    app, db, http
):
    """``update_session_delivery_tally`` runs INSIDE the collection transaction;
    if it started raising on a missing session every orderless collection would
    500 at the door."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 10)
    headers = {"Authorization": f"Bearer {_token(app, driver)}"}
    body = {"customer_id": u.id, "address_id": a.id, "quantity": 3}

    # (a) no session at all — the collection must still succeed.
    assert http.post("/api/v1/staff/bottles/collection", json=body,
                     headers=headers).status_code == 200
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("7.00"), Decimal("7.00"))

    # (b) with an open session, the tally moves by exactly the collected qty.
    session = BottleTrackingService().open_bottle_session(driver.id, 20)
    db.session.commit()
    before = session.bottles_collected_from_customers or 0
    assert http.post("/api/v1/staff/bottles/collection", json=body,
                     headers=headers).status_code == 200
    _db.session.expire_all()
    refreshed = BottleTrackingService().get_open_session(driver.id)
    assert (refreshed.bottles_collected_from_customers or 0) == before + 3
    assert _pair(a.id) == (Decimal("4.00"), Decimal("4.00"))


# ===========================================================================
# 7. Fines
# ===========================================================================


def _run_full_fine(monkeypatch, bridge, context, handler, customer_id, address_id,
                   qty="2", amount="50000", note="two missing"):
    fine_update, _ = _make_update_context(
        callback_data=f"staff_bottle_fine_{customer_id}_{address_id}"
    )
    asyncio.run(handler.start_fine(fine_update, context))
    for text, method in ((qty, handler.receive_fine_bottle_qty),
                         (amount, handler.receive_fine_amount),
                         (note, handler.receive_fine_note)):
        step, _ = _make_update_context(message_text=text)
        asyncio.run(method(step, context))
    return fine_update


def test_fine_payload_is_keyed_by_address_and_carries_no_bottle_balance_id(
    app, db, http, monkeypatch
):
    """Migration a3e7d1f9c204 dropped ``bottle_balance_id``; the old lookup always
    failed, so EVERY driver-issued fine bailed to a generic error.

    The body gained a SIXTH key on 2026-08-03: the per-intent idempotency token
    minted in ``receive_fine_amount``. Still a whole-body assertion, but the
    token's VALUE is random, so it is asserted by shape (``uuid4().hex``).
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 6)

    bridge = _Bridge(http, _token(app, driver))
    _, context, handler = _show_statement(monkeypatch, bridge, u.id)
    _run_full_fine(monkeypatch, bridge, context, handler, u.id, a.id)

    posts = bridge.posted("/bottles/fine")
    assert len(posts) == 1
    payload = posts[0]["payload"]
    assert set(payload) == {
        "customer_id", "address_id", "quantity", "fine_amount", "notes",
        "idempotency_key",
    }
    assert payload["customer_id"] == u.id
    assert payload["address_id"] == a.id
    assert payload["quantity"] == 2
    assert payload["fine_amount"] == 50000.0
    assert payload["notes"] == "two missing"
    # NOT bottle_balance_id, and NOT a hard-coded token.
    assert re.fullmatch(r"[0-9a-f]{32}", payload["idempotency_key"])


def test_fine_prompt_hint_has_four_distinct_arms(app, db, http, monkeypatch, i18n_spy):
    """Both keys deliberately share the ``{union}`` kwarg name: renaming one makes
    ``str.format`` raise, and ``staff_bot/i18n.py`` catches that and prints the
    RAW template to the driver."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    token = _token(app, driver)

    def _hint(setup):
        u = _customer(db)
        addr = setup(u)
        bridge = _Bridge(http, token)
        _, context, handler = _show_statement(monkeypatch, bridge, u.id)
        update, _ = _make_update_context(callback_data=f"staff_bottle_fine_{u.id}_{addr.id}")
        asyncio.run(handler.start_fine(update, context))
        return _edited_text(update)

    def _grouped(u, balance):
        mine = _address(db, u, title="office")
        theirs = _address(db, _customer(db), title="office")
        _group(db, [mine, theirs], admin)
        if balance > 0:
            _deliver(db, u, mine, balance)
        elif balance < 0:
            _deliver(db, u, mine, 1)
            _collect(db, u, mine, 1 - balance, driver)
        return mine

    positive = _hint(lambda u: _grouped(u, 6))
    assert "staff.delivery.fine_place_union_hint" in positive
    assert _kwargs_for(i18n_spy, "staff.delivery.fine_place_union_hint") == {"union": "6"}

    i18n_spy.clear()
    negative = _hint(lambda u: _grouped(u, -3))
    assert "staff.delivery.fine_place_over_returned_hint" in negative
    assert "staff.delivery.fine_place_union_hint" not in negative
    assert _kwargs_for(i18n_spy, "staff.delivery.fine_place_over_returned_hint") == {"union": "3"}

    i18n_spy.clear()
    zero = _hint(lambda u: _grouped(u, 0))
    assert "fine_place_union_hint" not in zero
    assert "fine_place_over_returned_hint" not in zero

    i18n_spy.clear()

    def _solo(u):
        addr = _address(db, u, title="home")
        _deliver(db, u, addr, 6)
        return addr

    ungrouped = _hint(_solo)
    assert "fine_place_union_hint" not in ungrouped
    assert "fine_place_over_returned_hint" not in ungrouped


def test_fine_prompt_still_works_when_user_data_was_lost(app, db, http, monkeypatch):
    """A bot restart mid-shift must not make every visible Fine button dead: the
    ids come from the CALLBACK, and only the hint is lost."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 6)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data.pop("pending_bottle_collection_flow", None)

    prompt = _run_full_fine(monkeypatch, bridge, context, handler, u.id, a.id)

    assert "fine_place_union_hint" not in _edited_text(prompt)
    assert len(bridge.posted("/bottles/fine")) == 1
    _db.session.expire_all()
    assert BottleFine.query.count() == 1


def test_real_route_fine_is_place_scoped_and_moves_no_bottles(app, db, http, monkeypatch):
    """A fine is an AUDIT EVENT, not a bottle movement. Writing it with a
    non-zero quantity would mint bottles; writing it through the decoupled
    writer would create drift. Assert the PAIR is untouched."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    group = _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 5)
    before = _pair(a1.id)

    bridge = _Bridge(http, _token(app, driver))
    _, context, handler = _show_statement(monkeypatch, bridge, u1.id)
    _run_full_fine(monkeypatch, bridge, context, handler, u1.id, a1.id)
    _db.session.expire_all()

    assert _pair(a1.id) == before == (Decimal("5.00"), Decimal("5.00"))
    fine = BottleFine.query.one()
    assert fine.address_id == a1.id
    assert fine.address_group_id == group.id
    assert fine.user_id == u1.id
    assert fine.status == BottleFineStatus.PENDING

    entry = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_ISSUED).one()
    assert entry.quantity == Decimal("0.00")
    assert entry.balance_after == Decimal("5.00")
    assert entry.entry_metadata["place_balance_at_issue"] == 5.0
    assert entry.address_group_id == group.id


def test_fine_at_an_over_returned_place_is_allowed(app, db, http):
    """Collect is HIDDEN at an over-returned place precisely so Fine stays
    reachable; a "cannot fine a place with no shortage" guard would make the
    screen's only remaining action unusable."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)
    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))

    resp = http.post(
        "/api/v1/staff/bottles/fine",
        json={"customer_id": u.id, "address_id": a.id, "quantity": 2, "fine_amount": 50000},
        headers={"Authorization": f"Bearer {_token(app, driver)}"},
    )
    assert resp.status_code == 200, resp.get_json()
    _db.session.expire_all()

    entry = BottleLedger.query.filter_by(event_type=BottleLedgerEventType.FINE_ISSUED).one()
    assert entry.entry_metadata["place_balance_at_issue"] == -3.0
    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))


@pytest.mark.parametrize("text,accepted", [
    ("0", None), ("-2", None), ("3.5", None), ("abc", None), ("  4  ", 4),
])
def test_fine_quantity_input_validation(app, db, http, monkeypatch, text, accepted):
    """``int()`` raises on "3.5"; a permissive ``float()`` cast would post a
    fractional fine quantity the route accepts."""
    from staff_bot.handlers.delivery.bottle_collection import BOTTLE_FINE_QTY_INPUT

    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": u.id, "address_id": a.id, "action": "fine",
    }
    update, _ = _make_update_context(message_text=text)
    state = asyncio.run(handler.receive_fine_bottle_qty(update, context))

    flow = context.user_data["pending_bottle_collection_flow"]
    if accepted is None:
        assert state == BOTTLE_FINE_QTY_INPUT
        assert update.message.reply_text.call_args.args[0] == "Invalid bottle count"
        assert "fine_quantity" not in flow
    else:
        assert flow["fine_quantity"] == accepted


@pytest.mark.parametrize("text,accepted", [
    ("0", None), ("-5", None), ("abc", None), ("50 000", 50000.0), ("50,000", 50000.0),
    # ``-inf`` was already rejected before the finiteness fence landed:
    # float('-inf') <= 0 is True, so the plain sign guard caught it. NaN and
    # +Infinity were the two that slipped through — see the test below.
    ("-inf", None),
])
def test_fine_amount_input_validation(app, db, http, monkeypatch, text, accepted):
    from staff_bot.handlers.delivery.bottle_collection import BOTTLE_FINE_AMOUNT_INPUT

    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": u.id, "address_id": a.id, "action": "fine", "fine_quantity": 2,
    }
    update, _ = _make_update_context(message_text=text)
    state = asyncio.run(handler.receive_fine_amount(update, context))

    flow = context.user_data["pending_bottle_collection_flow"]
    if accepted is None:
        assert state == BOTTLE_FINE_AMOUNT_INPUT
        assert "fine_amount" not in flow
    else:
        assert flow["fine_amount"] == accepted


@pytest.mark.parametrize("text", ["nan", "inf", "Infinity"])
def test_fine_amount_rejects_nan_and_infinity(app, db, http, monkeypatch, text):
    """FIXED — the xfail is gone.

    WAS: ``receive_fine_amount`` did ``float(text)`` then ``if amount <= 0:
    raise``. NaN and Infinity both make that comparison False, so both were
    accepted and posted as the non-standard JSON literals Python's json module
    happily emits AND re-parses. They then diverged downstream and NEITHER
    outcome was acceptable: ``Decimal('NaN') <= 0`` raises
    ``decimal.InvalidOperation`` inside ``issue_fine`` (a 500 at the customer's
    door), while ``Decimal('Infinity') <= 0`` is merely False, so an INFINITE
    fine was accepted, persisted, and read back to the next driver as
    "Active fines: 1 (inf Uzs)".

    NOW the amount goes through ``_parse_positive_amount``, which coerces with
    ``Decimal`` (the same fence the backend's SSOT ``_as_decimal`` uses) and
    checks ``is_finite()`` BEFORE any ordering comparison — Python's decimal is
    not IEEE-754, so comparing ``Decimal('NaN')`` RAISES. The backend half
    (``test_the_fine_route_rejects_a_non_finite_fine_amount``) is what stops it
    reaching money if any other caller ever slips.

    (``-inf`` is NOT in this list: it was already rejected by the old ``<= 0``
    guard — see ``test_fine_amount_input_validation``.)
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u)

    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": u.id, "address_id": a.id, "action": "fine", "fine_quantity": 2,
    }
    update, _ = _make_update_context(message_text=text)
    asyncio.run(handler.receive_fine_amount(update, context))

    assert "fine_amount" not in context.user_data["pending_bottle_collection_flow"]


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_the_fine_route_rejects_a_non_finite_fine_amount(app, db, http, literal):
    """FIXED — the xfail is gone.

    WAS (the backend half of the same defect): `POST /staff/bottles/fine` reads
    `fine_amount` straight off `request.get_json()` with no finiteness fence —
    there is no serializer on this route AT ALL — and Python's json module
    parses the non-standard `NaN` / `Infinity` literals the bot's `float()`
    produced. `issue_fine` then evaluated `Decimal('NaN') <= 0`, which RAISES
    `decimal.InvalidOperation` (a 500 at the customer's door), while
    `Decimal('Infinity') <= 0` is merely False, so the infinite fine was
    COMMITTED: `BottleFine.fine_amount = Infinity`, and the next driver's
    statement header rendered it as "Active fines: 1 (inf Uzs)".

    NOW both are a 400 with no fine row — and note WHY, because it is the whole
    argument for where the guard went: this route has no serializer, so the
    HTTP-boundary `allow_inf_nan=False` added to the three ADMIN request models
    does nothing for it. What saves it is
    `BottleTrackingService._as_decimal`, the SSOT coercion every bottle write
    already funnels through. A per-route fix would have left this route open.

    The bot-level half (`test_fine_amount_rejects_nan_and_infinity`) is a
    SEPARATE defect in `receive_fine_amount`'s own `float(text)` + `<= 0` check
    and stays pinned — the backend fence below is what stops it reaching money.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    # Sent as raw text so the wire literal is the subject, not json.dumps' float
    # repr — this is byte-for-byte what StaffAPIClient puts on the socket.
    resp = http.post(
        "/api/v1/staff/bottles/fine",
        data=(f'{{"customer_id": {u.id}, "address_id": {a.id}, '
              f'"quantity": 2, "fine_amount": {literal}}}'),
        headers={"Authorization": f"Bearer {_token(app, driver)}",
                 "Content-Type": "application/json"},
    )

    assert resp.status_code == 400, (resp.status_code, resp.get_data(as_text=True))
    _db.session.expire_all()
    assert BottleFine.query.count() == 0
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))


def test_fine_note_step_bails_when_the_flow_lost_its_ids(app, db, http, monkeypatch):
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    bridge = _Bridge(http, _token(app, driver))
    handler = _bottle_handler(monkeypatch, bridge)
    _, context = _make_update_context()
    context.user_data["pending_bottle_collection_flow"] = {
        "action": "fine", "fine_quantity": 2, "fine_amount": 50000,
    }
    update, _ = _make_update_context(message_text="note")

    asyncio.run(handler.receive_fine_note(update, context))

    assert bridge.posted("/bottles/fine") == []
    assert update.message.reply_text.call_args.args[0] == "Error occurred"
    assert "pending_bottle_collection_flow" not in context.user_data


def test_a_fine_started_mid_collect_flow_posts_only_the_fine(app, db, http, monkeypatch):
    """``start_collection`` and ``start_fine`` mutate the SAME flow dict, and the
    text router dispatches purely on which keys are set. Entering the fine flow
    with a stale ``quantity`` still present must post exactly one fine."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)

    bridge = _Bridge(http, _token(app, driver))
    _, context, handler = _show_statement(monkeypatch, bridge, u.id)
    collect, _ = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(collect, context))
    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{a.id}_3")
    asyncio.run(handler.pick_collection_qty(pick, context))
    assert context.user_data["pending_bottle_collection_flow"]["quantity"] == 3

    _run_full_fine(monkeypatch, bridge, context, handler, u.id, a.id,
                   qty="1", amount="20000", note="one missing")

    assert len(bridge.posted("/bottles/fine")) == 1
    assert bridge.posted("/bottles/fine")[0]["payload"]["quantity"] == 1
    assert bridge.posted("/bottles/collection") == []
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))


def test_re_entering_collect_clears_a_stale_quantity(app, db, http, monkeypatch):
    """FIXED — the xfail is gone.

    WAS: ``start_collection`` set customer_id/address_id/action/balance on the
    EXISTING flow dict and never cleared a stale ``quantity`` left by an
    abandoned pick. The global text router finalises a collection for ANY typed
    text while ``action == 'collect'`` and ``quantity`` is not None, so
    re-entering Collect — possibly at a DIFFERENT address — turned the driver's
    next message into a completed collection of a quantity they never picked.

    NOW ``_begin_flow`` starts a FRESH dict on entry (both here and in
    ``start_fine``), carrying over only the two cached balance maps.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    first, second = _address(db, u, title="first"), _address(db, u, title="second")
    _deliver(db, u, first, 5)
    _deliver(db, u, second, 5)

    bridge = _Bridge(http, _token(app, driver))
    _, context, handler = _show_statement(monkeypatch, bridge, u.id)

    start, _ = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{first.id}")
    asyncio.run(handler.start_collection(start, context))
    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{first.id}_3")
    asyncio.run(handler.pick_collection_qty(pick, context))

    # The driver abandons and taps Collect at the OTHER door.
    again, _ = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{second.id}")
    asyncio.run(handler.start_collection(again, context))

    assert context.user_data["pending_bottle_collection_flow"].get("quantity") is None

    # ...and the CONSEQUENCE, which is what actually reaches a customer's door:
    # with the new picker still on screen the driver's next message must not
    # book anything. (bot.py:1113-1118 dispatches straight to this method once
    # `quantity` is set; calling it here is that router's own hot path.)
    typed, _ = _make_update_context(message_text="wrong door")
    asyncio.run(handler.receive_collection_note(typed, context))
    assert bridge.posted("/bottles/collection") == []
    _db.session.expire_all()
    assert _pair(first.id) == (Decimal("5.00"), Decimal("5.00"))
    assert _pair(second.id) == (Decimal("5.00"), Decimal("5.00"))


# ===========================================================================
# 8. At-door return prompt
# ===========================================================================


def test_at_door_prompt_positive_arm_offers_three_options_anchored_on_the_place(
    app, db, http, monkeypatch, i18n_spy
):
    """The anchor is ``customer_bottle_balance`` (the CLAMPED place balance, a
    coworker's empties included), never ``expected_returnable_bottles``.

    ``i18n_spy`` is load-bearing: ``staff_bot/i18n.py`` humanises a missing key's
    last segment and DROPS every kwarg, so a bare substring assertion on the
    rendered copy would pass or fail purely on whether the seed ran.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 2)
    _deliver(db, u2, a2, 2)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u1, a1, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    snapshot = context.user_data["current_delivery"]
    assert snapshot["customer_bottle_balance"] == 4.0
    # The order only delivers ONE returnable bottle — so a prompt anchored on
    # the order rather than the place would quote 1, not 4.
    assert snapshot["expected_returnable_bottles"] == 1.0

    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    text = _edited_text(update)
    assert "staff.delivery.bottles_return_prompt" in text
    assert _kwargs_for(i18n_spy, "staff.delivery.bottles_return_prompt") == {"balance": 4}
    callbacks = _callbacks(_edited_markup(update))
    assert callbacks == [f"staff_bottles_full_{delivery.id}",
                         f"staff_bottles_custom_{delivery.id}",
                         f"staff_bottles_none_{delivery.id}"]


def test_at_door_prompt_zero_arm_drops_the_none_returned_row(
    app, db, http, monkeypatch, i18n_spy
):
    """The zero keyboard REUSES ``staff_bottles_full_``; re-adding the "None
    returned" row would give the driver two buttons submitting the same thing."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    product = _bottle_product(db, per_unit="2")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    assert "staff.delivery.bottles_return_prompt_no_balance" in _edited_text(update)
    assert _callbacks(_edited_markup(update)) == [
        f"staff_bottles_full_{delivery.id}", f"staff_bottles_custom_{delivery.id}",
    ]


def test_at_door_prompt_over_returned_arm_names_the_state(
    app, db, http, monkeypatch, i18n_spy
):
    """Without the SIGNED field the driver is told "no empties are on record" —
    factually wrong: there IS a record and it is negative."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    snapshot = context.user_data["current_delivery"]
    assert snapshot["customer_bottle_balance"] == 0.0
    assert snapshot["place_bottle_balance_signed"] == -3.0

    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    assert "staff.delivery.bottles_return_prompt_over_returned" in _edited_text(update)
    assert _kwargs_for(
        i18n_spy, "staff.delivery.bottles_return_prompt_over_returned"
    ) == {"count": "3"}
    assert _callbacks(_edited_markup(update)) == [
        f"staff_bottles_full_{delivery.id}", f"staff_bottles_custom_{delivery.id}",
    ]


def test_tapping_all_on_the_over_returned_arm_submits_zero(app, db, http, monkeypatch):
    """``confirm_full_bottle_return`` submits ``_get_suggested_return_count()``;
    if that ever returned the SIGNED value, a negative ``bottles_returned``
    would reach ``record_bottles_returned`` (which rejects it) — or be negated
    into a mint."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)
    product = _bottle_product(db, per_unit="1")
    order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=2)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    handler = _status_handler(monkeypatch, bridge)
    tap, _ = _make_update_context(callback_data=f"staff_bottles_full_{delivery.id}")
    asyncio.run(handler.confirm_full_bottle_return(tap, context))
    _db.session.expire_all()

    put = [c for c in bridge.calls if c["method"] == "PUT"][-1]
    assert put["payload"]["metadata"]["bottles_returned"] == 0
    # The delivery recorded +2 and NO return entry: -3 + 2 == -1, on BOTH figures.
    assert _pair(a.id) == (Decimal("-1.00"), Decimal("-1.00"))
    assert BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
    ).count() == 0
    assert BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").count() == 1


def test_tapping_all_on_the_positive_arm_submits_the_place_anchor(
    app, db, http, monkeypatch
):
    """``order_service`` records the DELIVERY before the RETURN, so anchoring on
    ``expected_returnable_bottles`` would submit 2 and leave 5 stale empties at
    the door. Conservation pair: 5 + 2 − 5 == 2 on both figures."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 5)
    product = _bottle_product(db, per_unit="1")
    order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=2)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    assert context.user_data["current_delivery"]["expected_returnable_bottles"] == 2.0
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    handler = _status_handler(monkeypatch, bridge)
    tap, _ = _make_update_context(callback_data=f"staff_bottles_full_{delivery.id}")
    asyncio.run(handler.confirm_full_bottle_return(tap, context))
    _db.session.expire_all()

    put = [c for c in bridge.calls if c["method"] == "PUT"][-1]
    assert put["payload"]["metadata"]["bottles_returned"] == 5
    assert _pair(a.id) == (Decimal("2.00"), Decimal("2.00"))
    assert BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").count() == 1
    assert BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
    ).one().quantity == Decimal("-5.00")


def test_the_prompt_never_appears_when_the_order_has_no_returnable_items(
    app, db, http, monkeypatch
):
    """``_get_expected_bottles`` is the GATE and ``_get_suggested_return_count``
    the ANCHOR; swapping them either suppresses the prompt where it is needed or
    shows it on every water-free order."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)
    product = _bottle_product(db, tracks=False)
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=3)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    assert context.user_data["current_delivery"]["expected_returnable_bottles"] == 0

    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)
    _db.session.expire_all()

    put = [c for c in bridge.calls if c["method"] == "PUT"][-1]
    assert "bottles_returned" not in put["payload"].get("metadata", {})
    assert "bottles_return_prompt" not in _edited_text(update)
    assert _pair(a.id) == (Decimal("-3.00"), Decimal("-3.00"))


@pytest.mark.parametrize("delivered,collected,clamped,signed", [
    (1, 4, 0.0, -3.0),
    (2, 2, 0.0, 0.0),
    (4, 0, 4.0, 4.0),
])
def test_the_signed_place_balance_survives_the_active_delivery_whitelist(
    app, db, http, monkeypatch, delivered, collected, clamped, signed
):
    """``current_delivery`` is an explicit WHITELIST. Omit the key and the
    over-returned prompt silently never fires, with every other test green."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, delivered)
    if collected:
        _collect(db, u, a, collected, driver)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)

    snapshot = context.user_data["current_delivery"]
    assert snapshot["customer_bottle_balance"] == clamped
    assert snapshot["place_bottle_balance_signed"] == signed


def test_a_snapshot_taken_before_the_signed_field_shipped_degrades_gracefully(
    app, db, http, monkeypatch, i18n_spy
):
    """``_get_place_signed_balance`` uses ``.get(..., 0) or 0``; a direct
    subscript would crash mid-delivery for every driver whose card was rendered
    before the deploy.

    ``i18n_spy`` echoes the KEY, so the arm is named outright instead of being
    compared against a second call to the same production helper (which agreed
    with itself whichever key it picked).
    """
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    handler = StatusUpdateHandler.__new__(StatusUpdateHandler)
    _, context = _make_update_context()
    context.user_data["current_delivery"] = {
        "expected_returnable_bottles": 2, "customer_bottle_balance": 0,
    }

    keyboard, message = handler._build_bottle_prompt("en", 55, context)
    assert message == "staff.delivery.bottles_return_prompt_no_balance"
    assert not any(c["key"] == "staff.delivery.bottles_return_prompt_over_returned"
                   for c in i18n_spy)
    # ...and the ZERO keyboard, not the positive three-row one.
    assert _callbacks(keyboard) == ["staff_bottles_full_55", "staff_bottles_custom_55"]


def test_backend_pins_the_clamped_and_signed_pair_for_every_address_shape(
    app, db, http
):
    """``_customer_bottle_balance`` clamps with ``max(0, …)`` and
    ``_place_bottle_balance_signed`` must NOT. A shared-helper refactor would
    clamp both and delete the over-returned state from the whole bot."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u1, u2, u3 = _customer(db), _customer(db), _customer(db)
    grouped = _address(db, u1, title="office")
    coworker = _address(db, u2, title="office")
    _group(db, [grouped, coworker], admin)
    solo = _address(db, u3, title="home")
    _deliver(db, u1, grouped, 1)
    _collect(db, u1, grouped, 4, driver)
    _deliver(db, u3, solo, 4)

    product = _bottle_product(db, per_unit="1")
    _o1, d1 = _delivery_for(db, u1, grouped, driver, product=product)
    _o2, d2 = _delivery_for(db, u3, solo, driver, product=product)
    # ...and an order with NO delivery address at all.
    order3 = Order(user_id=u2.id, order_number=f"ORD-NOADDR-{_next()}",
                   status=OrderStatus.OUT_FOR_DELIVERY,
                   subtotal=Decimal("1.00"), delivery_fee=Decimal("0.00"),
                   discount_amount=Decimal("0.00"), loyalty_discount=Decimal("0.00"),
                   total_amount=Decimal("1.00"), payment_method=PaymentMethod.CARD)
    db.session.add(order3)
    db.session.flush()
    d3 = Delivery(order_id=order3.id, delivery_person_id=driver.id,
                  status=DeliveryStatus.ARRIVED, scheduled_date=datetime.now(UTC),
                  scheduled_time_slot="09:00-12:00")
    db.session.add(d3)
    db.session.commit()

    resp = http.get("/api/v1/staff/delivery/active",
                    headers={"Authorization": f"Bearer {_token(app, driver)}"})
    assert resp.status_code == 200, resp.get_json()
    items = {i["delivery_id"]: i for i in resp.get_json()["data"]["items"]}

    assert items[d1.id]["customer_bottle_balance"] == 0.0
    assert items[d1.id]["place_bottle_balance_signed"] == -3.0
    assert items[d2.id]["customer_bottle_balance"] == 4.0
    assert items[d2.id]["place_bottle_balance_signed"] == 4.0
    assert items[d3.id]["customer_bottle_balance"] == 0.0
    assert items[d3.id]["place_bottle_balance_signed"] == 0.0


def test_custom_at_door_count_accepts_zero(app, db, http, monkeypatch):
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 2)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    handler = _status_handler(monkeypatch, bridge)
    custom, _ = _make_update_context(callback_data=f"staff_bottles_custom_{delivery.id}")
    asyncio.run(handler.start_custom_bottle_return(custom, context))
    typed, _ = _make_update_context(message_text="0")
    asyncio.run(handler.receive_bottle_count(typed, context))
    _db.session.expire_all()

    assert BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
    ).count() == 0
    assert _pair(a.id) == (Decimal("3.00"), Decimal("3.00"))


def test_custom_at_door_count_has_no_upper_bound(app, db, http, monkeypatch):
    """CURRENT BEHAVIOUR, PINNED — and reported as a defect.

    ``receive_bottle_count`` validates only "is a non-negative int": no upper
    bound and no comparison against the place anchor the prompt just stated. A
    fat-fingered 300 at a door holding 3 is recorded verbatim and drives the
    place to −297 with no confirmation step, while the STANDALONE collection
    path constrains the same driver to a capped picker. Whether a challenge
    dialog or a hard cap is the right answer is a product decision, so this pins
    what happens today rather than pre-empting it."""
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 2)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    handler = _status_handler(monkeypatch, bridge)
    custom, _ = _make_update_context(callback_data=f"staff_bottles_custom_{delivery.id}")
    asyncio.run(handler.start_custom_bottle_return(custom, context))
    typed, _ = _make_update_context(message_text="300")
    asyncio.run(handler.receive_bottle_count(typed, context))
    _db.session.expire_all()

    # 2 + 1 delivered - 300 returned. Both figures move together, so this is a
    # data-entry defect, not a conservation one.
    assert _pair(a.id) == (Decimal("-297.00"), Decimal("-297.00"))


@pytest.mark.parametrize("text", ["-1", "abc", "2.5"])
def test_custom_at_door_count_rejects_bad_input_without_losing_the_flow(
    app, db, http, monkeypatch, text
):
    """The router dispatches on ``awaiting_bottle_count``; clearing it on a
    validation failure would drop the driver's next number into the menu router."""
    from staff_bot.handlers.delivery.status_update import BOTTLE_RETURN_INPUT

    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, 4)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)
    handler = _status_handler(monkeypatch, bridge)
    custom, _ = _make_update_context(callback_data=f"staff_bottles_custom_{delivery.id}")
    asyncio.run(handler.start_custom_bottle_return(custom, context))

    bad, _ = _make_update_context(message_text=text)
    state = asyncio.run(handler.receive_bottle_count(bad, context))
    assert state == BOTTLE_RETURN_INPUT
    assert bad.message.reply_text.call_args.args[0] == "Invalid bottle count"
    assert context.user_data["pending_delivery_cash_flow"]["awaiting_bottle_count"] is True

    good, _ = _make_update_context(message_text="2")
    asyncio.run(handler.receive_bottle_count(good, context))
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("3.00"), Decimal("3.00"))


def test_a_positive_fractional_place_is_not_announced_as_no_record(
    app, db, http, monkeypatch, i18n_spy
):
    """FIXED — the xfail is gone.

    WAS: ``_get_suggested_return_count`` did
    ``int(float(customer_bottle_balance))``, so a place holding 0 < b < 1
    yielded a suggestion of 0 and ``_build_bottle_prompt`` then found
    ``signed >= 0`` and rendered "no empties are on record for this customer" —
    factually wrong, and the exact mirror of the bug the over-returned arm was
    added to fix.

    NOW the anchor keeps the fraction (integral balances still come back as
    ``int`` so the prompt reads "All 4 returned", never "All 4.0 returned" —
    see ``test_at_door_prompt_positive_arm_offers_three_options_anchored_on_the_place``,
    which pins the kwarg as the number 4).
    """
    # ``i18n_spy`` echoes the KEY back. Without it the humaniser renders
    # "Bottles return prompt no balance" and the literal key never appears in the
    # message, so the assertion below could not fail even against the old bug.
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u)
    _deliver(db, u, a, "0.5")
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    assert context.user_data["current_delivery"]["place_bottle_balance_signed"] == 0.5
    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    assert "staff.delivery.bottles_return_prompt_no_balance" not in _edited_text(update)


def test_the_at_door_prompt_is_anchored_on_the_delivery_that_was_tapped(
    app, db, http, monkeypatch, i18n_spy
):
    """FIXED — the xfail is gone.

    WAS: ``_maybe_show_bottle_prompt_or_submit`` / ``_build_bottle_prompt`` /
    ``confirm_full_bottle_return`` / ``execute_status_change`` all read
    ``context.user_data['current_delivery']`` WITHOUT comparing it to the
    ``delivery_id`` in the callback. Each active-delivery card is its own
    message, so a driver who opened B and then acted on A's older card got A's
    completion driven by B's anchor — "All N returned" posting B's N against A's
    place, on a screen titled with B's order number.

    NOW ``_anchor_current_delivery`` compares the snapshot against the tapped id
    and re-reads the tapped delivery from ``/delivery/active`` on a mismatch
    (refusing outright when it is no longer there).
    """
    # ``i18n_spy`` echoes the KEY, which is what lets this name the arm outright.
    # Without it the humaniser renders "Bottles return prompt over returned" and
    # the underscored key never appears, i.e. the assertion could not pass even
    # against correct code.
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u_a, u_b = _customer(db), _customer(db)
    addr_a, addr_b = _address(db, u_a, title="A"), _address(db, u_b, title="B")
    _deliver(db, u_a, addr_a, 1)
    _collect(db, u_a, addr_a, 4, driver)          # place A at -3
    _deliver(db, u_b, addr_b, 5)                  # place B at +5
    product = _bottle_product(db, per_unit="1")
    order_a, delivery_a = _delivery_for(db, u_a, addr_a, driver, product=product)
    order_b, delivery_b = _delivery_for(db, u_b, addr_b, driver, product=product)

    bridge = _Bridge(http, _token(app, driver))
    # Two cards, two Telegram messages, ONE `context.user_data` — which is
    # exactly what PTB gives every handler for this driver.
    _open_delivery_card(monkeypatch, bridge, delivery_a.id)
    context = _open_delivery_card(monkeypatch, bridge, delivery_b.id)

    # ...and now the driver goes back to A's ORIGINAL detail message.
    update, _ = _reach_bottle_prompt(monkeypatch, bridge, context, delivery_a.id)
    text = _edited_text(update)

    # The screen is headed by `_order_brief`, which reads the same stale
    # snapshot: today A's completion screen is titled with B's ORDER NUMBER.
    # Asserted as an ABSENCE so it holds under either fix (re-fetch the tapped
    # delivery, or refuse to act on a mismatched snapshot).
    assert order_b.order_number not in text, text
    # A's place is at -3, so A's completion screen must name the over-returned
    # state. Today it renders B's positive anchor (5) instead — the wrong door.
    assert "staff.delivery.bottles_return_prompt_over_returned" in text
    assert not any(c["key"] == "staff.delivery.bottles_return_prompt" for c in i18n_spy)
    assert order_a.order_number in text, text


# ===========================================================================
# 9. The place lifecycle, seen from the driver's screen
# ===========================================================================


def test_statement_and_picker_after_a_split_with_bottles_leaving(app, db, http):
    """The split is the ONLY membership edit that moves bottles. Conservation is
    asserted across BOTH resulting places, before and after."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 4)
    _deliver(db, u2, a2, 3)
    assert _pair(a1.id) == (Decimal("7.00"), Decimal("7.00"))

    CustomerLinkService().remove_address_from_group(
        a1.id, acting_admin_id=admin.id, reason="moved out", bottles_leaving=2
    )
    _db.session.expire_all()

    leaver, stayer = _pair(a1.id), _pair(a2.id)
    assert leaver == (Decimal("2.00"), Decimal("2.00"))
    assert stayer == (Decimal("5.00"), Decimal("5.00"))
    assert leaver[0] + stayer[0] == Decimal("7.00")     # conservation
    assert leaver[1] + stayer[1] == Decimal("7.00")

    token = _token(app, driver)
    mine = _summary(http, token, u1.id)
    theirs = _summary(http, token, u2.id)
    assert [s["balance"] for s in mine["cluster_scopes"]] == [2.0]
    assert [s["balance"] for s in theirs["cluster_scopes"]] == [5.0]
    assert BottleCollectionHandler._format_bottle_statement(mine, "en").count("5") == 0
    assert [r["place_balance"] for r in _address_rows(http, token, u1.id)] == [2.0]
    assert [r["place_balance"] for r in _address_rows(http, token, u2.id)] == [5.0]


def test_statement_after_a_split_with_the_default_zero_bottles_leaving(app, db, http):
    """§8's netting is RETIRED: by default all bottles stay with the place and
    the departing address starts a fresh scope at 0. If netting ever returns the
    leaver gains a phantom NEGATIVE place, which the ``!= 0`` filter would then
    make actionable and finable."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 7)

    result = CustomerLinkService().remove_address_from_group(
        a1.id, acting_admin_id=admin.id, reason="moved out"
    )
    _db.session.expire_all()
    assert result["bottles_leaving"] == Decimal("0.00")
    assert "netting" not in result

    token = _token(app, driver)
    # BOTH sides, BOTH figures. A split that moved the stored balance without
    # re-stamping the ledger (or the reverse) conserves the 7 bottles globally
    # while attributing them to the wrong place — invisible to a single-sided
    # assertion and to any total taken across the two scopes.
    assert _pair(a1.id) == (Decimal("0.00"), Decimal("0.00"))
    assert _pair(a2.id) == (Decimal("7.00"), Decimal("7.00"))
    assert _address_rows(http, token, u1.id) == []
    mine = _summary(http, token, u1.id)
    assert BottleCollectionHandler._actionable_places(mine) == []
    assert [r["place_balance"] for r in _address_rows(http, token, u2.id)] == [7.0]


def test_statement_after_the_place_dissolves_onto_its_last_member(app, db, http):
    """The memberless ``AddressGroup`` row is KEPT on purpose (an FK from
    ``bottle_ledger`` makes deleting it impossible). If ``get_customer_scopes``
    ever matched on it, the driver would be shown a phantom place at a door
    nobody lives at."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    group = _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 4)
    _deliver(db, u2, a2, 3)

    CustomerLinkService().remove_address_from_group(
        a1.id, acting_admin_id=admin.id, reason="moved out"
    )
    _db.session.expire_all()

    survivor = _db.session.get(UserAddress, a2.id)
    assert survivor.address_group_id is None                 # dissolved
    assert _pair(a2.id) == (Decimal("7.00"), Decimal("7.00"))
    # The LEAVER'S side of the same pair. All 7 stayed, so a1's fresh scope must
    # hold nothing on either figure — a dissolve that left a1 a stored row it
    # has no ledger for is exactly the drift only the §7.4 review can close.
    assert _pair(a1.id) == (Decimal("0.00"), Decimal("0.00"))
    assert BottleBalance.query.filter_by(address_group_id=group.id).count() == 0

    token = _token(app, driver)
    assert [r["place_balance"] for r in _address_rows(http, token, u2.id)] == [7.0]
    assert [s["balance"] for s in _summary(http, token, u2.id)["cluster_scopes"]] == [7.0]
    assert _address_rows(http, token, u1.id) == []


def test_statement_after_a_join_absorbs_the_joining_addresss_own_balance(app, db, http):
    """§7.2 exists because split→re-add STRANDED bottles. If the absorb
    regresses, the driver's statement under-reports by the joiner's balance and
    those bottles are uncollectable from the bot."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2, u3 = _customer(db), _customer(db), _customer(db)
    g1, g2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    group = _group(db, [g1, g2], admin)
    _deliver(db, u1, g1, 4)
    joiner = _address(db, u3, title="office")
    _deliver(db, u3, joiner, 3)
    # The PAIR on both sides, before the join. The join moves the stored figure
    # by the joiners' STORED balances (``absorb_address_into_group``'s carry) and
    # the place's ledger sum by the joiners' RE-STAMPED ledger rows — two
    # independently computed quantities. Pinning both pre-join is what localises
    # a post-join mismatch to the join rather than to the fixture writes.
    assert _pair(group_addr := g1.id) == (Decimal("4.00"), Decimal("4.00"))
    assert _pair(joiner.id) == (Decimal("3.00"), Decimal("3.00"))

    CustomerLinkService().add_addresses_to_group(
        group.id, [joiner.id], acting_admin_id=admin.id, reason="same office"
    )
    _db.session.expire_all()

    def _dump():
        return {
            "balances": [(b.id, b.address_id, b.address_group_id, str(b.balance))
                         for b in BottleBalance.query.all()],
            "ledger": [(e.id, e.address_id, e.address_group_id, e.event_type,
                        str(e.quantity), str(e.balance_after), e.idempotency_key)
                       for e in BottleLedger.query.order_by(BottleLedger.id).all()],
            "users": User.query.count(),
            "addresses": [(a.id, a.user_id, a.address_group_id)
                          for a in UserAddress.query.all()],
        }

    assert _pair(group_addr) == (Decimal("7.00"), Decimal("7.00")), _dump()
    assert _pair(joiner.id) == (Decimal("7.00"), Decimal("7.00")), _dump()
    # The joiner's own-scope row is gone — nothing stranded outside the place.
    assert BottleBalance.query.filter_by(address_id=joiner.id,
                                         address_group_id=None).count() == 0

    token = _token(app, driver)
    for user in (u1, u3):
        assert [s["balance"] for s in _summary(http, token, user.id)["cluster_scopes"]] == [7.0]
        assert [r["place_balance"] for r in _address_rows(http, token, user.id)] == [7.0]


def test_a_stranded_own_scope_row_for_a_grouped_address_is_invisible_to_the_driver(
    app, db, http
):
    """DOCUMENTING A PRE-PLAN-C DATA SHAPE (the one case where the balance ROW is
    the subject). ``get_customer_scopes`` builds ``solo_ids`` only from addresses
    whose ``address_group_id IS NULL``, so a leftover own-scope row belonging to
    an address that IS grouped matches neither clause: the driver is offered the
    group's figure and the stranded bottles are unreachable from every staff
    surface. §7.2's absorb closes this AT JOIN TIME, which is why the row is
    built here by hand — a join can no longer produce it."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    a1, a2 = _address(db, u1, title="office"), _address(db, u2, title="office")
    _group(db, [a1, a2], admin)
    _deliver(db, u1, a1, 5)

    stranded = BottleBalance(address_id=a1.id, address_group_id=None, balance=Decimal("20.00"))
    db.session.add(stranded)
    db.session.commit()

    token = _token(app, driver)
    rows = _address_rows(http, token, u1.id)
    assert [r["place_balance"] for r in rows] == [5.0]
    assert [s["balance"] for s in _summary(http, token, u1.id)["cluster_scopes"]] == [5.0]
    # The 20 exists in the database and no staff surface can reach it.
    assert _db.session.get(BottleBalance, stranded.id).balance == Decimal("20.00")


def test_statement_after_a_reviewed_merge_with_a_resulting_balance_override(
    app, db, http, monkeypatch
):
    """The driver reads the STORED balance. After a reviewed merge
    ``get_place_balance == ledger_sum``, so the header, the body line, the picker
    label and the quantity cap all read the same explainable number — and the
    next collection cannot re-open the drift."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u1, u2 = _customer(db), _customer(db)
    drifted = _address(db, u1, title="office")
    clean = _address(db, u2, title="office")
    _drift_place_to(db, drifted, 20)
    _deliver(db, u2, clean, 2)

    _group(db, [drifted, clean], admin, resulting_balance=Decimal("12"))
    _db.session.expire_all()

    assert _pair(drifted.id) == (Decimal("12.00"), Decimal("12.00"))

    token = _token(app, driver)
    summary = _summary(http, token, u1.id)
    text = BottleCollectionHandler._format_bottle_statement(summary, "en")
    assert _total_line(text).endswith(": 12")
    assert any(": 12" in line for line in _body_lines(text))

    rows = _address_rows(http, token, u1.id)
    assert rows[0]["place_balance"] == 12.0
    label = DeliveryKeyboards.bottle_address_selection("en", u1.id, rows).inline_keyboard[0][0].text
    assert "(12)" in label

    bridge = _Bridge(http, token)
    handler = _bottle_handler(monkeypatch, bridge)
    update, context = _make_update_context(
        callback_data=f"staff_bottle_collect_{u1.id}_{rows[0]['address_id']}"
    )
    asyncio.run(handler.start_collection(update, context))
    assert context.user_data["pending_bottle_collection_flow"]["balance"] == 12


def test_a_destructive_admin_reconcile_between_render_and_tap_lets_the_picker_over_collect(
    app, db, http, monkeypatch
):
    """``reconcile_balance`` is DESTRUCTIVE BY CONSTRUCTION: it assigns
    ``balance = ledger_sum`` unconditionally, writes NO ledger entry and only
    logs a warning. Plan C never calls it, but ``POST /admin/bottles/reconcile``
    still exposes it — and nothing in the collection path re-validates the
    PICKED quantity against a fresh balance. The one thing that must hold is
    that the driver is TOLD the resulting state, not handed a bare minus."""
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER)
    u = _customer(db)
    a = _address(db, u, title="drifted")
    _drift_place_to(db, a, 20)

    token = _token(app, driver)
    bridge = _Bridge(http, token)
    handler = _bottle_handler(monkeypatch, bridge)
    render, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(render, context))
    assert f"staff_bottle_qty_{u.id}_{a.id}_20" in _callbacks(_edited_markup(render))

    reconciled = http.post(
        f"/api/v1/admin/bottles/reconcile/{a.id}",
        headers={"Authorization": f"Bearer {_token(app, admin)}"},
    )
    assert reconciled.status_code == 200, reconciled.get_json()
    _db.session.expire_all()
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))   # destroyed, no ledger row

    pick, _ = _make_update_context(callback_data=f"staff_bottle_qty_{u.id}_{a.id}_20")
    asyncio.run(handler.pick_collection_qty(pick, context))
    note, _ = _make_update_context(message_text="took twenty")
    asyncio.run(handler.receive_collection_note(note, context))
    _db.session.expire_all()

    assert _pair(a.id) == (Decimal("-20.00"), Decimal("-20.00"))
    receipt = note.message.reply_text.call_args.args[0]
    assert "-20" not in receipt
    assert "Bottle collection recorded over returned" in receipt


# ===========================================================================
# 10. Translations — the four over-returned keys, rendered for real
# ===========================================================================


def _load_seed(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seeded_i18n(monkeypatch):
    """Install the REAL seed values into the i18n catalog for en/uz/ru.

    ``staff_bot/i18n.py`` catches ``str.format`` errors and prints the RAW
    template, so a mistyped placeholder ships a template string to a driver at a
    customer's door with nothing logged as an error. Rendering the shipped
    values with the kwargs the handlers actually pass is the only check that
    catches it.
    """
    catalog = {}
    for module_name in ("seed_staff_over_returned_translations",):
        for key, langs in _load_seed(module_name).KEYS.items():
            for lang, value in langs.items():
                catalog.setdefault(lang, {})[key] = value
    monkeypatch.setattr(i18n, "translations", catalog)
    return catalog


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_whole_over_returned_journey_renders_in_every_language(
    app, db, http, monkeypatch, seeded_i18n, language
):
    """Six surfaces, one place at −3, three languages. Every one must show the
    MAGNITUDE and never a minus sign or a leftover brace.

    The place is GROUPED on purpose: ``_place_balances`` populates the fine
    prompt's hint map only from rows whose ``is_grouped`` is truthy, so an
    ungrouped place renders surface 4 with no hint at all and the over-returned
    copy would never be exercised there.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u, coworker = _customer(db), _customer(db)
    a = _address(db, u, title="office")
    _group(db, [a, _address(db, coworker, title="office")], admin)
    _deliver(db, u, a, 1)
    _collect(db, u, a, 4, driver)

    token = _token(app, driver)
    bridge = _Bridge(http, token)
    handler = _bottle_handler(monkeypatch, bridge, language=language)

    surfaces = []

    # 1. statement body + header
    summary = _summary(http, token, u.id)
    surfaces.append(BottleCollectionHandler._format_bottle_statement(summary, language))

    # 2. picker label
    rows = _address_rows(http, token, u.id)
    surfaces.append(
        DeliveryKeyboards.bottle_address_selection(language, u.id, rows).inline_keyboard[0][0].text
    )

    # 3. quantity guard
    guard, context = _make_update_context(callback_data=f"staff_bottle_collect_{u.id}_{a.id}")
    asyncio.run(handler.start_collection(guard, context))
    surfaces.append(_edited_text(guard))

    # 4. fine hint
    _, fine_context, fine_handler = _show_statement(monkeypatch, bridge, u.id, language=language)
    hint, _ = _make_update_context(callback_data=f"staff_bottle_fine_{u.id}_{a.id}")
    asyncio.run(fine_handler.start_fine(hint, fine_context))
    surfaces.append(_edited_text(hint))

    # 5. at-door prompt
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    prompt_handler = StatusUpdateHandler.__new__(StatusUpdateHandler)
    _, at_door = _make_update_context()
    at_door.user_data["current_delivery"] = {
        "expected_returnable_bottles": 1,
        "customer_bottle_balance": 0.0,
        "place_bottle_balance_signed": -3.0,
    }
    _kb, message = prompt_handler._build_bottle_prompt(language, 55, at_door)
    surfaces.append(message)

    # 6. collection receipt
    surfaces.append(i18n.get(
        "staff.delivery.bottle_collection_recorded_over_returned", language,
        quantity=1, remaining="3",
    ))

    import re

    assert len(surfaces) == 6
    for rendered in surfaces:
        # The MAGNITUDE as a whole number, not "3" anywhere in the string: the
        # at-door copy already carries "18.9 L" / "18,9 л", so a bare substring
        # test would keep passing on copy that dropped the count entirely.
        assert re.search(r"(?<!\d)3(?!\d)", rendered), (language, rendered)
        assert "-3" not in rendered, (language, rendered)
        assert "{" not in rendered and "}" not in rendered, (language, rendered)
    # ...and each surface renders the magnitude the way ITS OWN surface should,
    # so one falling back to a generic string that merely contains a 3 is caught.
    expected = seeded_i18n[language]["staff.delivery.place_over_returned"].format(count="3")
    assert "\u21a93" in surfaces[1], (language, surfaces[1])   # picker label
    assert surfaces[2] == expected, (language, surfaces[2])     # quantity guard
    assert expected in surfaces[0], (language, surfaces[0])     # statement total + body
    assert surfaces[4] == seeded_i18n[language][
        "staff.delivery.bottles_return_prompt_over_returned"
    ].format(count="3"), (language, surfaces[4])


def test_every_over_returned_key_is_seeded_with_identical_placeholders(seeded_i18n):
    """A dropped placeholder in one language is a number the driver never sees."""
    import re

    keys = _load_seed("seed_staff_over_returned_translations").KEYS
    for key, langs in keys.items():
        assert set(langs) == {"en", "uz", "ru"}, key
        placeholders = {
            lang: set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))
            for lang, value in langs.items()
        }
        assert len(set(map(frozenset, placeholders.values()))) == 1, (key, placeholders)


def test_staff_health_reports_the_over_returned_keys_as_missing_when_unseeded(
    monkeypatch
):
    """``/health`` enumerates every literal ``staff.*`` key used under
    ``staff_bot/`` and 503s while any is missing. A deploy that restarts the bot
    BEFORE seeding must fail loudly rather than render raw templates for a whole
    shift."""
    keys = set(_load_seed("seed_staff_over_returned_translations").KEYS)
    catalog = {lang: {k: "x" for k in keys} for lang in ("en", "uz", "ru")}
    catalog["uz"].pop("staff.delivery.place_over_returned")
    monkeypatch.setattr(i18n, "translations", catalog)

    missing = i18n.get_missing_translation_keys()
    assert "staff.delivery.place_over_returned" in set(missing.get("uz", []))
    assert "staff.delivery.place_over_returned" not in set(missing.get("en", []))


# ===========================================================================
# 11. Gap hunt — RETRIED writes and the POOLED at-door anchor
#
# Every scenario below satisfies global conservation AND per-place
# ``get_place_balance == ledger_sum``. That is precisely why the rest of this
# file (and the other ten) is blind to them: the damage is misattribution and
# double-counting, not a number that fails to add up. So each test asserts
# PER-SCOPE attribution — how many rows exist, which scope holds them, which
# figure the driver's screen was anchored on at each moment — and, where the
# oracle really is blind, asserts the blindness EXPLICITLY so it is recorded
# rather than assumed.
#
# The shared entry point is ``_Bridge.retry_suffixes``: "the backend committed
# and then the response was lost" is an ordinary event on a doorstep with flaky
# mobile data — not a hypothetical. As of 2026-08-03 the api_client no longer
# re-sends a POST after an ambiguous failure (``RETRY_SAFE_METHODS`` is
# GET/HEAD/PUT), so the duplicate no longer originates there — but ``_Bridge``
# keeps emitting one on purpose, because what these tests pin is the SERVER-side
# fence, which must hold against a duplicate from any client, proxy or replay.
# ===========================================================================


def _open_card_in(monkeypatch, bridge, context, delivery_id):
    """``view_active_delivery`` into an EXISTING context.

    ``_open_delivery_card`` mints a fresh context per call, which quietly gives
    every delivery its own ``current_delivery`` slot. The real bot has ONE
    ``user_data`` per driver and ``current_delivery`` is a single overwritten
    key — that difference is the whole subject of the stale-anchor test below.
    """
    handler = _active_handler(monkeypatch, bridge)
    update, _ = _make_update_context(callback_data=f"staff_view_active_{delivery_id}")
    asyncio.run(handler.view_active_delivery(update, context))
    return update


def _tap_all_returned(monkeypatch, bridge, context, delivery_id):
    """The driver's "All N returned" tap on an already-shown bottle prompt."""
    handler = _status_handler(monkeypatch, bridge)
    tap, _ = _make_update_context(callback_data=f"staff_bottles_full_{delivery_id}")
    asyncio.run(handler.confirm_full_bottle_return(tap, context))
    return tap


def _last_put(bridge):
    return [c for c in bridge.calls if c["method"] == "PUT"][-1]


def _ledger_sum(event_type):
    return _db.session.query(
        func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00"))
    ).filter(BottleLedger.event_type == event_type).scalar()


def _keyed_sum(*keys):
    """Ledger sum over an explicit set of idempotency keys.

    Summing by event type would fold the fixture's seeding deliveries into the
    at-door ones; keying on ``delivery:{order}`` names exactly the two writes the
    two doorsteps produced.
    """
    return _db.session.query(
        func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00"))
    ).filter(BottleLedger.idempotency_key.in_(keys)).scalar()


def _sweep():
    """The nightly invariant sweep, run for real."""
    from business_app.tasks.customer_link_tasks import reconcile_customer_link_invariants

    return {k: v for k, v in reconcile_customer_link_invariants().items() if v}


# -- 11.1  A retried fine POST -----------------------------------------------


def _issue_fine_through_the_bot(monkeypatch, bridge, customer, address, qty, amount):
    _, context, handler = _show_statement(monkeypatch, bridge, customer.id)
    _run_full_fine(
        monkeypatch, bridge, context, handler, customer.id, address.id,
        qty=str(qty), amount=str(amount), note="three missing",
    )


def test_a_retried_fine_post_must_not_issue_the_fine_twice(app, db, http, monkeypatch):
    """FIXED — the xfail is gone. One doorstep shortage, one lost response, ONE
    fine and ONE bottle debit.

    WAS: ``issue_fine`` wrote an unkeyed ``BottleFine`` AND an unkeyed
    FINE_ISSUED ledger entry. ``waive_fine`` and ``mark_fine_paid`` are both
    fenced by their status check; ``issue_fine`` was fenced by nothing — so a
    fine the backend had already committed was issued a SECOND time, and paying
    both debited the place twice for one shortage while billing the customer
    twice for it.

    NOW the bot mints a per-intent token in ``receive_fine_amount`` and
    ``_Bridge`` re-sends the SAME dict object (:154-158), so the second POST
    carries the same token, ``issue_fine`` recognises the replay and returns the
    ORIGINAL fine at HTTP 200 — the driver sees a success, because that is what
    it is. This is BUG 16's defect class (a write path with no idempotency key)
    on the one path that ALSO carries money.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 5)
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))

    bridge = _Bridge(http, _token(app, driver))
    bridge.retry_suffixes = ["/bottles/fine"]
    _issue_fine_through_the_bot(monkeypatch, bridge, u, a, 3, 30000)
    _db.session.expire_all()

    # The retry is invisible to the driver: two POSTs, one tap.
    assert len(bridge.posted("/bottles/fine")) == 2
    assert BottleFine.query.count() == 1
    assert BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.FINE_ISSUED
    ).count() == 1

    for fine in BottleFine.query.all():
        BottleTrackingService().mark_fine_paid(fine.id, driver.id, notes="settled")
    db.session.commit()
    _db.session.expire_all()

    # 5 − 3 == 2. The shortage was three bottles ONCE.
    assert _pair(a.id) == (Decimal("2.00"), Decimal("2.00"))
    assert _summary(http, _token(app, driver), u.id)["total_fine_amount"] == 0.0


def test_a_deduped_duplicate_fine_bills_once_and_every_oracle_agrees(
    app, db, http, monkeypatch
):
    """REWRITTEN 2026-08-03. This test used to be marked PINNING TODAY'S WRONG
    NUMBERS and it asserted the world its own docstring demanded a fix change:
    two fines, two FINE_ISSUED entries, 60,000 UZS billed for one shortage, and a
    place at 2.00 — with every oracle this repo owns reporting clean.

    That blindness was the point: the place is seeded at 8.00 rather than the
    5.00 of its sibling above precisely so the double-debit landed on a POSITIVE
    2.00, invisible to ``reconcile_customer_link_invariants``'s
    ``negative_place_balances`` arm too. NOTHING could see the damage, so nothing
    but an idempotency key could have prevented it.

    Now there is one fine, one FINE_ISSUED entry, one 30,000 UZS charge and a
    place at 5.00. The oracles are kept — not because they would have caught the
    old bug (they provably would not), but because the DEDUP itself must not
    create drift: swallowing the ledger row while still moving the stored balance
    (or vice versa) is exactly the single-sided write ``_pair`` exists to catch.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 8)

    bridge = _Bridge(http, _token(app, driver))
    bridge.retry_suffixes = ["/bottles/fine"]
    _issue_fine_through_the_bot(monkeypatch, bridge, u, a, 3, 30000)
    _db.session.expire_all()

    # Two POSTs went out; one fine came back.
    assert len(bridge.posted("/bottles/fine")) == 2
    fines = BottleFine.query.order_by(BottleFine.id).all()
    assert len(fines) == 1
    assert [f.quantity for f in fines] == [Decimal("3.00")]
    assert BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.FINE_ISSUED
    ).count() == 1

    token = _token(app, driver)
    # ...and the driver at the NEXT door is told the customer owes it ONCE.
    billed = _summary(http, token, u.id)
    assert billed["active_fines_count"] == 1
    assert billed["total_fine_amount"] == 30000.0

    for fine in fines:
        BottleTrackingService().mark_fine_paid(fine.id, driver.id, notes="settled")
    db.session.commit()
    _db.session.expire_all()

    # One FINE_PAID, carrying its own server-derived key.
    paid = BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.FINE_PAID
    ).order_by(BottleLedger.id).all()
    assert [e.idempotency_key for e in paid] == [f"fine_paid:{fines[0].id}"]

    # -- and now every oracle, all of them clean and now telling the TRUTH ----
    assert _pair(a.id) == (Decimal("5.00"), Decimal("5.00"))   # 8 − 3, once
    assert _sweep() == {}                                      # nightly sweep
    reconciled = BottleTrackingService().reconcile_balance(a.id)
    assert reconciled["discrepancy"] == 0                      # ledger replay
    # The driver's screen agrees with itself too: total (cluster_scopes) and
    # body (addresses[].place_balance) both read the correct figure.
    summary = _summary(http, token, u.id)
    assert summary["cluster_scopes"][0]["balance"] == 5.0
    assert summary["addresses"][0]["place_balance"] == 5.0


# -- 11.2  A retried at-door delivery PUT ------------------------------------


def _deliver_at_the_door_returning(monkeypatch, bridge, context, delivery_id, count):
    """Custom at-door count, typed by the driver, submitted for real."""
    handler = _status_handler(monkeypatch, bridge)
    start, _ = _make_update_context(callback_data=f"staff_bottles_custom_{delivery_id}")
    asyncio.run(handler.start_custom_bottle_return(start, context))
    typed, _ = _make_update_context(message_text=str(count))
    asyncio.run(handler.receive_bottle_count(typed, context))
    return typed


def _replies(update):
    return [c.args[0] if c.args else c.kwargs.get("text")
            for c in update.message.reply_text.call_args_list]


def test_a_retried_at_door_delivery_put_records_the_bottles_exactly_once(
    app, db, http, monkeypatch
):
    """The MONEY and BOTTLE halves survive the retry — only the driver does not.

    Two fences do the work: ``idempotency_key='delivery:{order}'`` /
    ``'return:{order}:{delivery}'`` on the ledger, and the terminal-status guard
    (``DELIVERY_STATUS_TRANSITIONS['delivered'] == []``) which rejects the second
    PUT before any write is attempted. Pinning this here is what makes the xfail
    below strictly about what the DRIVER is shown.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 4)
    product = _bottle_product(db, per_unit="1")
    order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    bridge.retry_suffixes = [f"/delivery/{delivery.id}/status"]
    _deliver_at_the_door_returning(monkeypatch, bridge, context, delivery.id, 2)
    _db.session.expire_all()

    puts = [c for c in bridge.calls if c["method"] == "PUT"]
    assert len(puts) == 2
    assert puts[0]["payload"] == puts[1]["payload"]            # byte-identical retry
    assert puts[0]["payload"]["metadata"]["bottles_returned"] == 2

    # 4 + 1 − 2 == 3, on BOTH figures, exactly once.
    assert _pair(a.id) == (Decimal("3.00"), Decimal("3.00"))
    assert BottleLedger.query.filter_by(idempotency_key=f"delivery:{order.id}").count() == 1
    assert BottleLedger.query.filter_by(
        idempotency_key=f"return:{order.id}:{delivery.id}"
    ).count() == 1
    assert Order.query.get(order.id).status == OrderStatus.DELIVERED
    assert Delivery.query.get(delivery.id).status == DeliveryStatus.DELIVERED
    # The order is billed once and settled once.
    assert Payment.query.filter_by(order_id=order.id).count() == 1


def test_a_retried_at_door_delivery_put_must_not_report_failure_to_the_driver(
    app, db, http, monkeypatch, i18n_spy
):
    """FIXED — the xfail is gone. The most common real outcome of a flaky
    connection at a door.

    WAS: a delivery PUT that COMMITTED and then timed out is retried by
    ``StaffAPIClient._make_request`` against a now-DELIVERED order, which 400s
    with STAFF_INVALID_STATUS_TRANSITION. ``_submit_delivery_completion``
    rendered that as a failure ('staff.error.api.invalid_input') and returned
    WITHOUT calling ``_clear_delivery_cash_flow`` — so the driver was told a
    completed, already-billed delivery had failed (their next action is to
    redeliver or call the operator about an order whose bottles are already on
    the truck) and the at-door flow stayed armed.

    NOW that one error code is treated as the idempotent success it is: the
    only 'delivered' transition reachable from this screen is the one this
    method just submitted, so a refusal here means it is already recorded. The
    completion is acknowledged and the flow cleared. The sibling test above
    pins that the money and bottle halves were already exactly-once.
    """
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 4)
    product = _bottle_product(db, per_unit="1")
    _order, delivery = _delivery_for(db, u, a, driver, product=product, quantity=1)

    bridge = _Bridge(http, _token(app, driver))
    context = _open_delivery_card(monkeypatch, bridge, delivery.id)
    _reach_bottle_prompt(monkeypatch, bridge, context, delivery.id)

    bridge.retry_suffixes = [f"/delivery/{delivery.id}/status"]
    typed = _deliver_at_the_door_returning(monkeypatch, bridge, context, delivery.id, 2)

    # The delivery DID succeed, so nothing the driver is shown may say otherwise.
    # ``i18n_spy`` echoes the key, so this names the arm outright instead of
    # depending on whether the staff translation seed has run.
    shown = _replies(typed)
    assert shown, "the driver was shown nothing at all"
    assert not any(str(m).startswith("❌") for m in shown), shown
    assert not any("staff.error.api." in str(m) for m in shown), shown

    # ...and BUG 15's mechanism: the error arm returns before the flow is cleared,
    # so the next thing the driver taps runs against a stale at-door flow.
    assert context.user_data.get("pending_delivery_cash_flow") is None


# -- 11.3  The half of a duplicated collection that no admin can repair ------


def test_a_duplicate_collection_leaves_the_drivers_session_tally_correct(
    app, db, http
):
    """FIXED — the xfail is gone. L2's acceptance criterion.

    WAS: ``record_standalone_collection`` called ``update_session_delivery_tally``
    on EVERY call, including one whose ledger write had just been deduped, so a
    duplicated collection double-counted the driver's session as well as the
    place. ``admin_adjust_balance`` repairs the CUSTOMER's figure and no admin
    surface can touch ``bottles_collected_from_customers``, so the session closed
    with a fabricated surplus against the driver forever. That is the difference
    between a bug and an UNRECOVERABLE one, and it is why the key had to cover
    the TALLY, not just the ledger.

    NOW the tally is gated on ``created`` — the flag ``_create_ledger_entry``
    returns to say the row is genuinely new. ``_Bridge`` emits the duplicate
    directly (:154-158); the real client no longer does (``RETRY_SAFE_METHODS``)
    because what is under test here is the SERVER-side fence, not the transport.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 5)

    session = BottleTrackingService().open_bottle_session(driver.id, 20)
    db.session.commit()
    assert (session.bottles_collected_from_customers or 0) == 0

    # ONE tap, five bottles into the van; the response is lost and the identical
    # body — same intent token — arrives a second time.
    bridge = _Bridge(http, _token(app, driver))
    bridge.retry_suffixes = ["/bottles/collection"]
    resp = asyncio.run(bridge.record_bottle_collection(
        "tok", {"customer_id": u.id, "address_id": a.id, "quantity": 5,
                "notes": "took the lot",
                "idempotency_key": "c81d4fae7dec11d0a76500a0c91e6bf6"}
    ))
    assert resp.success is True
    assert len(bridge.posted("/bottles/collection")) == 2
    _db.session.expire_all()

    # The admin looks at the place, sees whatever it says, and corrects it to the
    # truth (zero empties left at the door). Computing the delta rather than
    # hard-coding +5 keeps this test honest under a fix that makes the retry a
    # no-op: then there is nothing to repair and the adjustment is skipped.
    stored, _ledger = _pair(a.id)
    if stored != Decimal("0.00"):
        BottleTrackingService().admin_adjust_balance(
            u.id, a.id, Decimal("0.00") - stored, actor_user_id=admin.id,
            notes="reversing duplicate collection",
        )
        db.session.commit()
        _db.session.expire_all()
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))

    # The van physically carries the 20 it loaded plus the 5 it collected.
    closed = BottleTrackingService().close_bottle_session(driver.id, 25)
    db.session.commit()
    _db.session.expire_all()

    assert closed.bottles_collected_from_customers == 5
    assert closed.discrepancy == 0


def test_no_admin_surface_can_reach_a_double_counted_session_tally(app, db, http):
    """WHY THE TALLY HAD TO MOVE INSIDE THE FENCE — and it still asserts real
    numbers, because this body carries NO idempotency token.

    Kept unchanged through the 2026-08-03 retry fix on purpose. Its sibling above
    pins that a KEYED duplicate now tallies once; this one pins that an UNKEYED
    duplicate still behaves exactly as it always did — two ledger rows, two tally
    bumps — which is the backward-compatibility half of L2 and the reason
    ``created`` is ``True`` on every un-keyed write.

    What it documents is why that mattered: the reversing ADMIN_ADJUSTMENT is a
    balance write and touches no session counter, so once the two figures diverge
    they stay diverged. There is no admin surface that can reach
    ``bottles_collected_from_customers``, so a double-counted tally was permanent
    damage — asserted here rather than asserted-about.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    u = _customer(db)
    a = _address(db, u, title="home")
    _deliver(db, u, a, 5)
    BottleTrackingService().open_bottle_session(driver.id, 20)
    db.session.commit()

    bridge = _Bridge(http, _token(app, driver))
    bridge.retry_suffixes = ["/bottles/collection"]
    asyncio.run(bridge.record_bottle_collection(
        "tok", {"customer_id": u.id, "address_id": a.id, "quantity": 5}
    ))
    _db.session.expire_all()

    assert _pair(a.id) == (Decimal("-5.00"), Decimal("-5.00"))          # ← the defect
    assert BottleTrackingService().get_open_session(
        driver.id
    ).bottles_collected_from_customers == 10                            # ← its twin

    BottleTrackingService().admin_adjust_balance(
        u.id, a.id, Decimal("5.00"), actor_user_id=admin.id,
        notes="reversing duplicate collection",
    )
    db.session.commit()
    _db.session.expire_all()

    # The customer-facing figure is repaired...
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))
    # ...and the ADMIN_ADJUSTMENT wrote no session counter at all.
    assert BottleTrackingService().get_open_session(
        driver.id
    ).bottles_collected_from_customers == 10

    closed = BottleTrackingService().close_bottle_session(driver.id, 25)
    db.session.commit()
    # 20 loaded + 10 "collected" − 25 returned == a +5 surplus the driver never had.
    assert closed.discrepancy == 5
    # ...on a place, and a ledger, that agree with each other perfectly.
    assert _pair(a.id) == (Decimal("0.00"), Decimal("0.00"))
    assert _sweep() == {}


# -- 11.4  Two orders, one shared place, one trip ----------------------------


def _one_office_two_coworkers(db, driver, admin, *, seed_a=4, seed_b=3, per_order=3):
    """The archetypal shared-place delivery: one office, two phone numbers, one van."""
    ua, ub = _customer(db, first="Aziza"), _customer(db, first="Bek")
    a1 = _address(db, ua, title="office")
    a2 = _address(db, ub, title="office")
    group = _group(db, [a1, a2], admin)
    _deliver(db, ua, a1, seed_a)
    _deliver(db, ub, a2, seed_b)
    product = _bottle_product(db, per_unit="1")
    o1, d1 = _delivery_for(db, ua, a1, driver, product=product, quantity=per_order)
    o2, d2 = _delivery_for(db, ub, a2, driver, product=product, quantity=per_order)
    return group, (ua, a1, o1, d1), (ub, a2, o2, d2)


def test_two_orders_at_one_shared_place_double_count_the_pooled_all_returned_anchor(
    app, db, http, monkeypatch
):
    """PINNING TODAY'S WRONG NUMBERS. A fix must change this test.

    One office, seven empties, two coworkers' orders on one van. The driver taps
    "All returned" at each door, as designed. Ten empties are recorded as
    collected at a place that only ever held seven, and the three the driver
    personally carried in at the first stop are counted a second time at the
    second — they are physically FULL crates standing in the same room.

    Nothing refuses it, because over-return is a sanctioned state; the anchor is
    the PLACE's pooled balance (which is the whole point of pooling) and it is
    re-read between stops, so the second card is not stale — it is *correct* and
    still wrong. Every oracle passes: global conservation, per-place
    ``get_place_balance == ledger_sum``, and the nightly sweep.

    Every at-door test above this line exercises ONE delivery at ONE place, so
    this arc is structurally outside all of them.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    _group_row, (ua, a1, o1, d1), (ub, a2, o2, d2) = _one_office_two_coworkers(
        db, driver, admin
    )
    assert _pair(a1.id) == _pair(a2.id) == (Decimal("7.00"), Decimal("7.00"))

    token = _token(app, driver)
    bridge = _Bridge(http, token)

    # (a) ONE place, ONE pool: both cards quote the same seven empties.
    items = {i["delivery_id"]: i for i in http.get(
        "/api/v1/staff/delivery/active", headers={"Authorization": f"Bearer {token}"}
    ).get_json()["data"]["items"]}
    assert items[d1.id]["customer_bottle_balance"] == 7.0
    assert items[d2.id]["customer_bottle_balance"] == 7.0
    assert items[d1.id]["customer_id"] != items[d2.id]["customer_id"]

    # (b) first door — anchor 7, tap "All returned".
    ctx1 = _open_delivery_card(monkeypatch, bridge, d1.id)
    assert ctx1.user_data["current_delivery"]["customer_bottle_balance"] == 7.0
    assert ctx1.user_data["current_delivery"]["expected_returnable_bottles"] == 3.0
    _reach_bottle_prompt(monkeypatch, bridge, ctx1, d1.id)
    _tap_all_returned(monkeypatch, bridge, ctx1, d1.id)
    _db.session.expire_all()
    assert _last_put(bridge)["payload"]["metadata"]["bottles_returned"] == 7
    assert _pair(a1.id) == (Decimal("3.00"), Decimal("3.00"))   # 7 + 3 − 7

    # (c) second door, ten minutes later — the card is RE-FETCHED, so the anchor
    #     is the live 3.00. Those three crates are the ones this driver carried
    #     in at stop (b) and they are full.
    ctx2 = _open_delivery_card(monkeypatch, bridge, d2.id)
    assert ctx2.user_data["current_delivery"]["customer_bottle_balance"] == 3.0
    _reach_bottle_prompt(monkeypatch, bridge, ctx2, d2.id)
    _tap_all_returned(monkeypatch, bridge, ctx2, d2.id)
    _db.session.expire_all()
    assert _last_put(bridge)["payload"]["metadata"]["bottles_returned"] == 3

    # -- the arc --------------------------------------------------------------
    assert _keyed_sum(f"delivery:{o1.id}", f"delivery:{o2.id}") == Decimal("6.00")
    assert _ledger_sum(BottleLedgerEventType.RETURN_ON_DELIVERY) == Decimal("-10.00")
    # 7 + 6 − 10 == 3: ten empties collected from a place that held seven.
    assert _pair(a1.id) == _pair(a2.id) == (Decimal("3.00"), Decimal("3.00"))

    # Both returns are SCOPED to the place (``ledger_filter`` sums on
    # ``address_group_id`` for a grouped scope) while still recording the
    # physical door and the member whose order it was. The attribution is
    # right in every column; only the quantity is wrong — which is exactly why
    # a per-scope sum cannot see this and a per-scope AUDIT has to.
    returns = BottleLedger.query.filter_by(
        event_type=BottleLedgerEventType.RETURN_ON_DELIVERY
    ).order_by(BottleLedger.id).all()
    assert [r.address_group_id for r in returns] == [_group_row.id, _group_row.id]
    assert [r.address_id for r in returns] == [a1.id, a2.id]
    assert [r.user_id for r in returns] == [ua.id, ub.id]
    assert [r.idempotency_key for r in returns] == [
        f"return:{o1.id}:{d1.id}", f"return:{o2.id}:{d2.id}",
    ]

    # -- and every oracle is clean --------------------------------------------
    assert _sweep() == {}
    assert BottleTrackingService().reconcile_balance(a1.id)["discrepancy"] == 0


def test_a_reviewed_route_re_anchors_each_door_on_the_card_that_was_tapped(
    app, db, http, monkeypatch
):
    """FIXED — this test used to pin today's wrong numbers (−1.00, fourteen
    empties). The fix changed it, which is why it was written.

    The same trip as the sibling above, but the driver reviews the route first —
    opens both cards, then works the doors. ``current_delivery`` is still a
    SINGLE overwritten key in ``user_data`` (PTB gives a driver one
    ``user_data``, and each card is its own Telegram message), so the second
    card's snapshot still replaces the first's and the buttons on d1's message
    are still live afterwards.

    WAS: every handler read that one slot blind, so both doors submitted the
    SAME anchor of 7 — fourteen empties recorded at a place that held seven,
    landing at −1.00. Worse, ``_submit_delivery_completion`` stamped
    ``status='delivered'`` onto whatever snapshot happened to be loaded, so
    completing d1 marked d2's snapshot and the anchor never refreshed.

    NOW every at-door handler compares the loaded snapshot against the
    ``delivery_id`` in the callback and re-reads the TAPPED delivery from
    ``/delivery/active`` on a mismatch. Both doors therefore behave exactly as
    if each card had been opened immediately before it was worked: this arc
    collapses onto the sibling's, down to the ledger.

    What is left is NOT this bug: ten empties are still collected from a place
    that only ever held seven, because the anchor is the PLACE's pooled balance
    and the three crates the driver carried in at the first stop are counted
    again at the second. That defect is the sibling test's subject, it survives
    this fix, and no oracle sees it — the sweep is clean here precisely because
    the arithmetic no longer crosses zero.
    """
    admin = _staff(db, UserRole.ADMIN)
    driver = _staff(db, UserRole.DELIVERY_DRIVER, driver_profile=True)
    _group_row, (ua, a1, o1, d1), (ub, a2, o2, d2) = _one_office_two_coworkers(
        db, driver, admin
    )
    bridge = _Bridge(http, _token(app, driver))

    # The driver reviews the route: both cards opened, neither completed.
    context = _open_delivery_card(monkeypatch, bridge, d1.id)
    assert context.user_data["current_delivery"]["delivery_id"] == d1.id
    _open_card_in(monkeypatch, bridge, context, d2.id)
    snapshot = context.user_data["current_delivery"]
    assert snapshot["delivery_id"] == d2.id          # d1's slot is simply gone
    assert snapshot["customer_bottle_balance"] == 7.0

    # First door — the buttons on d1's message are still live, and the loaded
    # snapshot is d2's. The prompt re-anchors on d1 rather than trusting it.
    _reach_bottle_prompt(monkeypatch, bridge, context, d1.id)
    assert context.user_data["current_delivery"]["delivery_id"] == d1.id
    _tap_all_returned(monkeypatch, bridge, context, d1.id)
    _db.session.expire_all()
    assert _last_put(bridge)["path"].endswith(f"/delivery/{d1.id}/status")
    assert _last_put(bridge)["payload"]["metadata"]["bottles_returned"] == 7
    assert _pair(a1.id) == (Decimal("3.00"), Decimal("3.00"))

    # ...and the completion stamps the delivery it actually completed.
    assert context.user_data["current_delivery"]["delivery_id"] == d1.id
    assert context.user_data["current_delivery"]["status"] == "delivered"

    # Second door — re-anchored on d2, whose LIVE anchor is now 3.00.
    _reach_bottle_prompt(monkeypatch, bridge, context, d2.id)
    assert context.user_data["current_delivery"]["delivery_id"] == d2.id
    assert context.user_data["current_delivery"]["customer_bottle_balance"] == 3.0
    _tap_all_returned(monkeypatch, bridge, context, d2.id)
    _db.session.expire_all()
    assert _last_put(bridge)["path"].endswith(f"/delivery/{d2.id}/status")
    assert _last_put(bridge)["payload"]["metadata"]["bottles_returned"] == 3

    assert _keyed_sum(f"delivery:{o1.id}", f"delivery:{o2.id}") == Decimal("6.00")
    assert _ledger_sum(BottleLedgerEventType.RETURN_ON_DELIVERY) == Decimal("-10.00")
    assert _pair(a1.id) == _pair(a2.id) == (Decimal("3.00"), Decimal("3.00"))

    # THE CONTRAST, inverted: the arc no longer crosses zero, so the sweep is
    # blind to the pooled double-count that remains — exactly as it is blind to
    # the sibling above. ``negative_place_balances`` was never the detector for
    # this class; it only ever caught the stale-anchor arithmetic on top of it.
    assert _sweep() == {}
    assert BottleTrackingService().reconcile_balance(a1.id)["discrepancy"] == 0
    assert Order.query.get(o1.id).status == OrderStatus.DELIVERED
    assert Order.query.get(o2.id).status == OrderStatus.DELIVERED
