"""🔴 THE ADMIN SURFACE — the figure SHOWN and the scope POSTED are ONE decision.

THE THIRD INSTANCE OF ONE ROOT DEFECT. Twice already a number was computed for a
human while a scope was computed for the engine, with nothing forcing them to
describe the same set of debts:

  1. the driver's debtor row (a UNION) vs the staff-bot ceiling (a ``max``);
  2. the degraded bot ceiling (cluster-only) vs a still-PLACE-scoped post.

The third lived on the ADMIN cash-collection modal, gate ON, and neither prior
fix touched it. ``DeliveryReports.js`` posted ``places[0].address_id`` — PLACE
scope, settling ring 1 ∪ ring 2 — while rendering the raw per-account
``total_outstanding_amount``, and the route behind it called the frozen engine's
``get_customer_cod_statement`` directly, so no ceiling ever reached the admin.

Measured on the canonical A6 rows: shown **25 000**, posted address = Alice's
desk, true ceiling **45 000**. The admin collects the 25 000 they were shown,
**Alice still owes 10 000** and **10 000 of BOB's debt was paid instead**.

WHY EVERY TEST HERE MEASURES MONEY AND NOT A RENDERED NUMBER. A test asserting
only the displayed figure is exactly what missed this three times: in the
degraded instance the ceiling half was *correct* throughout, so a ceiling-only
assertion passed while the settlement went somewhere else. So each test below
posts the figure the admin is shown, through the REAL frozen engine, and then
asks whose debt actually moved and what became prepayment.

THE CANONICAL SCENARIO (owner ruling A6):
    Alice  10 000 at an ungrouped home  +  15 000 at office G
    Bob                                    20 000 at office G
    => place G owes 35 000; Alice's collect scope is 45 000, Bob's is 35 000.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.staff_service import StaffService
from shared import business_config
from shared.enums import OrderStatus
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _gate(app, monkeypatch):
    """Gate ON, and the Flask mirror restored afterwards — ``app`` is
    session-scoped (tests/conftest.py), so a bare assignment leaks into every
    later test on the same xdist worker."""
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    app.config["PLACE_COD_COLLECTION_ENABLED"] = True
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


@pytest.fixture
def office(db):
    """The A6 scenario, as rows."""
    alice, bob, admin = make_user(db), make_user(db), make_user(db)
    alice_home = make_address(db, alice)  # UNGROUPED
    alice_desk, bob_desk = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, alice_desk, bob_desk)
    delivered_cod_order(db, alice, address=alice_home, total=Decimal("10000.00"))
    delivered_cod_order(db, alice, address=alice_desk, total=Decimal("15000.00"))
    delivered_cod_order(db, bob, address=bob_desk, total=Decimal("20000.00"))
    return {
        "alice": alice,
        "bob": bob,
        "admin": admin,
        "group": group,
        "alice_home": alice_home,
        "alice_desk": alice_desk,
        "bob_desk": bob_desk,
    }


def _admin_statement(customer_id):
    """Exactly what ``GET /admin/staff/cash-reconciliation/customers/<id>/statement``
    now serves, i.e. what the modal receives."""
    return StaffService().get_customer_cod_statement_for_admin(customer_id)


def _shown_and_posted(customer_id):
    """The ONE published pair for a standalone (place-capable) collection: the
    figure the alert renders and the ``delivery_address_id`` the submit sends.

    Deliberately NOT two lookups — reading them from one dict is the whole point
    of the fix, and a test that recomposed either half would be re-asserting its
    own arithmetic instead of the shipped decision.
    """
    scope = _admin_statement(customer_id)["collect_scope"]
    return scope["amount"], scope["delivery_address_id"]


def _collect(office, customer_id, amount, delivery_address_id, source="standalone_meeting"):
    """Post through the REAL frozen engine, exactly as ``api/admin.py`` does."""
    return CashCollectionService().post_collection(
        customer_id=customer_id,
        amount=Decimal(str(amount)),
        source=source,
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=delivery_address_id,
        notes="admin recorded collection",
    )


def _owed(user_id):
    return CashCollectionService().get_customer_cod_statement(user_id)["total_outstanding_amount"]


def _credit(user_id):
    return Decimal(str(CashCollectionService().get_customer_prepaid_balance(user_id)))


# ---------------------------------------------------------------------------
# 🔴 THE PIN — the admin sees the figure its own submit settles
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_admin_figure_is_the_collect_scope_not_the_raw_account_total(app, db, office):
    """🔴 The two numbers that were allowed to disagree, side by side.

    ``total_outstanding_amount`` is the RAW per-account headline the modal used
    to render; ``collect_scope.amount`` is the union a place-scoped post
    actually settles. If a future edit ever makes these equal by accident this
    test still holds, because it asserts each against its own definition.
    """
    statement = _admin_statement(office["alice"].id)

    assert statement["total_outstanding_amount"] == 25000.0  # what was shown
    scope = statement["collect_scope"]
    assert scope["scope_type"] == "place"
    assert scope["amount"] == 45000.0  # what the submit settles
    assert scope["debt_count"] == 3  # her two + Bob's one
    assert scope["delivery_address_id"] == office["alice_desk"].id
    # The un-widened fallback travels with it, so no surface recomposes it.
    assert scope["cluster_amount"] == 25000.0
    assert scope["cluster_debt_count"] == 2


@pytest.mark.unit
def test_the_admin_figure_equals_the_ceiling_the_driver_is_offered(app, db, office):
    """One calculation, three surfaces. The admin modal, the staff bot's collect
    ceiling and the driver's debtor row must be the same number for the same
    person — that identity is what makes "shown == settled" a property rather
    than a coincidence."""
    staff_statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    place = next(p for p in staff_statement["places"] if p["place_group_id"] == office["group"].id)
    row = next(
        r
        for r in StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)["items"]
        if r.get("row_type") == "person"
        and office["alice"].id in (r.get("member_user_ids") or [r["id"]])
    )

    admin_amount, _ = _shown_and_posted(office["alice"].id)
    assert admin_amount == place["place_collect_ceiling_amount"] == row["total_outstanding_amount"]


@pytest.mark.unit
def test_collecting_the_figure_the_admin_is_shown_settles_exactly_what_it_names(app, db, office):
    """🔴 THE MONEY TEST. Do not delete.

    Collect the displayed amount with the posted scope and measure: every debt
    the figure counts must be gone, nothing must be left over, and no third
    party's balance may move.
    """
    shown, posted_address = _shown_and_posted(office["alice"].id)
    assert (shown, posted_address) == (45000.0, office["alice_desk"].id)

    event = _collect(office, office["alice"].id, shown, posted_address)
    db.session.refresh(event)

    assert event.scope_type == "place"
    assert _owed(office["alice"].id) == 0.0  # her home AND her desk
    assert _owed(office["bob"].id) == 0.0  # the coworker the figure counted
    assert float(CashCollectionService().get_place_open_cod_debt_total(office["group"].id)) == 0.0
    # Nothing left over: the figure was the whole set, not a slice of it.
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert _credit(office["alice"].id) == Decimal("0.00")
    assert _credit(office["bob"].id) == Decimal("0.00")


@pytest.mark.unit
def test_collecting_the_shown_figure_clears_the_named_customer_before_any_coworker(
    app, db, office
):
    """🔴 THE MONEY-ONLY PIN. It asserts NO displayed number whatsoever.

    A test that checks the rendered figure is exactly what missed this defect
    three times — in the degraded instance the displayed ceiling was *correct*
    and the settlement still went elsewhere. So this one only collects whatever
    the admin was shown and then asks the engine who still owes what.

    The invariant: a figure presented under one customer's name must clear that
    customer's own debt before a single som of it reaches a coworker. Under the
    shipped defect Alice was still 10 000 down after the admin collected "her"
    total, because 10 000 of it had gone to Bob.
    """
    shown, posted_address = _shown_and_posted(office["alice"].id)
    _collect(office, office["alice"].id, shown, posted_address)

    assert _owed(office["alice"].id) == 0.0, (
        "the admin collected the figure shown under Alice's name and Alice is still in debt "
        "— the money settled somebody else"
    )
    assert _owed(office["bob"].id) == 0.0


@pytest.mark.unit
def test_the_figure_the_admin_used_to_see_paid_a_coworkers_debt(app, db, office):
    """🔴 THE DEFECT, REPRODUCED AND MEASURED — the regression pin for why the
    two halves may never be re-split.

    This posts the OLD pair: the raw ``total_outstanding_amount`` the alert used
    to render, with the ``places[0].address_id`` the submit used to send. Both
    values still exist on the payload, so this reproduction stays honest.

    The admin is told "Outstanding: 25 000", collects exactly 25 000, and
    afterwards Alice — the person the figure named — STILL OWES 10 000, while
    10 000 of Bob's debt has been paid. The advertised total named one person
    and settled another. ``..._settles_exactly_what_it_names`` above is the same
    scenario with the shipped pair; the contrast is the test.
    """
    statement = _admin_statement(office["alice"].id)
    old_shown = statement["total_outstanding_amount"]
    old_posted_address = statement["places"][0]["address_id"]
    assert (old_shown, old_posted_address) == (25000.0, office["alice_desk"].id)

    _collect(office, office["alice"].id, old_shown, old_posted_address)

    assert _owed(office["alice"].id) == 10000.0  # named, and still in debt
    assert _owed(office["bob"].id) == 10000.0  # never named, and paid down
    assert _credit(office["alice"].id) == Decimal("0.00")


@pytest.mark.unit
def test_a_pending_order_does_not_inflate_the_admin_figure(app, db, office):
    """A PENDING cash order is in the per-account headline and in NO settlement:
    the engine's candidate rings are DELIVERED-only. The modal displayed 95 000
    where the collection could settle 45 000.

    Money check: collecting the published 45 000 clears the delivered union and
    leaves the pending order untouched, with nothing unapplied.
    """
    _order, pending_payment = delivered_cod_order(
        db, office["alice"], address=office["alice_home"],
        total=Decimal("70000.00"), status=OrderStatus.PENDING,
    )

    statement = _admin_statement(office["alice"].id)
    assert statement["total_outstanding_amount"] == 95000.0  # what was shown
    shown, posted_address = _shown_and_posted(office["alice"].id)
    assert shown == 45000.0

    event = _collect(office, office["alice"].id, shown, posted_address)
    db.session.refresh(event)
    db.session.refresh(pending_payment)

    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert Decimal(str(pending_payment.outstanding_amount)) == Decimal("70000.00")
    assert float(CashCollectionService().get_place_open_cod_debt_total(office["group"].id)) == 0.0


@pytest.mark.unit
def test_two_places_drop_the_address_and_show_the_cluster_figure(app, db, office):
    """Decision E7 — ambiguity must not be guessed. With two grouped places the
    modal can no more pick one than the driver's list can, so the scope degrades
    to cluster AND THE ADDRESS GOES WITH IT.

    Money check: the shown figure settles Alice's own two debts and Bob keeps
    every som of his.
    """
    second_desk = make_address(db, office["alice"])
    make_place_group(db, second_desk, label="other office")

    scope = _admin_statement(office["alice"].id)["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None
    assert scope["amount"] == 25000.0

    event = _collect(office, office["alice"].id, scope["amount"], scope["delivery_address_id"])
    db.session.refresh(event)

    assert event.scope_type in ("personal", "cluster")
    assert _owed(office["alice"].id) == 0.0
    assert _owed(office["bob"].id) == 20000.0  # untouched
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")


@pytest.mark.unit
def test_gate_off_publishes_cluster_scope_and_no_address(app, db, monkeypatch, office):
    """C0 rollback, WITH a pending order — the gate-off + PENDING combination
    that had no coverage anywhere.

    With the gate off no ceiling is composed, so the scope degrades to cluster,
    the address is dropped, and the figure shown is the delivered-only cluster
    debt (25 000) rather than the PENDING-inclusive headline (95 000). Money
    check: Bob is untouched.
    """
    delivered_cod_order(
        db, office["alice"], address=office["alice_home"],
        total=Decimal("70000.00"), status=OrderStatus.PENDING,
    )
    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    statement = _admin_statement(office["alice"].id)
    assert all("place_collect_ceiling_amount" not in p for p in statement["places"])
    scope = statement["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None
    assert scope["amount"] == 25000.0

    event = _collect(office, office["alice"].id, scope["amount"], scope["delivery_address_id"])
    db.session.refresh(event)

    assert event.scope_type in ("personal", "cluster")
    assert _owed(office["bob"].id) == 20000.0
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")


@pytest.mark.unit
def test_surplus_above_the_admin_figure_is_the_posting_customers_credit(app, db, office):
    """A6/R-D — surplus is per-user. One som above the published figure has no
    candidate debt left in either ring, so it must land as ALICE's prepaid
    credit and never touch Bob's balance."""
    shown, posted_address = _shown_and_posted(office["alice"].id)

    event = _collect(office, office["alice"].id, shown + 5000, posted_address)
    db.session.refresh(event)

    assert Decimal(str(event.unapplied_amount)) == Decimal("5000.00")
    assert _credit(office["alice"].id) == Decimal("5000.00")
    assert _credit(office["bob"].id) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 🔴 The entry point to that screen — the customer dropdown's search
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_search_row_is_the_admin_collect_scope(app, db, office):
    """``search_customers_for_cod_collection`` feeds the admin modal's customer
    dropdown, so its row is the doorway to the very screen above. It built rows
    from the RAW, PENDING-inclusive, per-account statement: Alice read
    (2 debts, 25 000) where the collection settles (3, 45 000)."""
    items = StaffService.search_customers_for_cod_collection(
        office["alice"].phone, "phone", only_with_open_cod=True
    )

    row = next(i for i in items if i["id"] == office["alice"].id)
    shown, _ = _shown_and_posted(office["alice"].id)
    assert row["total_outstanding_amount"] == shown == 45000.0
    assert row["active_cod_debt_count"] == 3


@pytest.mark.unit
def test_a_debt_free_coworker_at_an_indebted_place_is_searchable(app, db, office):
    """🔴 The row that did not exist. Carol owes nothing personally, so the
    person's OWN ``active_cod_debt_count`` filtered her out — and she is exactly
    the coworker holding the office's cash. Plan E R1 removed that per-person
    gate everywhere else.

    Money check: collecting her published figure settles the office — Alice's
    desk debt and Bob's — while Alice's UNGROUPED home debt, which Carol's
    figure never counted, survives untouched.
    """
    carol = make_user(db)
    carol_desk = make_address(db, carol)
    carol_desk.address_group_id = office["group"].id
    db.session.commit()

    items = StaffService.search_customers_for_cod_collection(
        carol.phone, "phone", only_with_open_cod=True
    )
    row = next(i for i in items if i["id"] == carol.id)
    assert row["active_cod_debt_count"] == 2
    assert row["total_outstanding_amount"] == 35000.0

    shown, posted_address = _shown_and_posted(carol.id)
    assert (shown, posted_address) == (35000.0, carol_desk.id)
    event = _collect(office, carol.id, shown, posted_address)
    db.session.refresh(event)

    assert _owed(office["bob"].id) == 0.0
    assert _owed(office["alice"].id) == 10000.0  # her ungrouped home debt only
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert _credit(carol.id) == Decimal("0.00")


@pytest.mark.unit
def test_search_is_the_raw_engine_row_when_the_gate_is_off(app, db, monkeypatch, office):
    """C0 — with the gate off the search is byte-for-byte Plan D: the raw
    per-account figures, and a debt-free coworker is filtered out again."""
    carol = make_user(db)
    carol_desk = make_address(db, carol)
    carol_desk.address_group_id = office["group"].id
    db.session.commit()

    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    alice_rows = StaffService.search_customers_for_cod_collection(
        office["alice"].phone, "phone", only_with_open_cod=True
    )
    row = next(i for i in alice_rows if i["id"] == office["alice"].id)
    assert row["total_outstanding_amount"] == 25000.0
    assert row["active_cod_debt_count"] == 2

    assert StaffService.search_customers_for_cod_collection(
        carol.phone, "phone", only_with_open_cod=True
    ) == []


# ---------------------------------------------------------------------------
# 🔴 The ungated scope input on the staff route
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_staff_route_drops_the_place_address_when_the_gate_is_off(
    app, db, client, monkeypatch, delivery_driver, office
):
    """``api/staff.py`` forwarded ``delivery_address_id`` UNGATED while
    ``api/admin.py`` nulled it. The gate is the rollback switch for the whole
    place feature, so with it off PLACE scope must be UNREACHABLE — and a direct
    API call reached it anyway.

    Measured, not asserted on the payload: post 45 000 with Alice's desk address
    while the gate is off. Bob must keep all 20 000 and the 20 000 that would
    have paid it must be Alice's prepaid credit instead.
    """
    from flask_jwt_extended import create_access_token

    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    with app.app_context():
        token = create_access_token(identity=str(delivery_driver.id))

    response = client.post(
        "/api/v1/staff/cash-collections",
        json={
            "customer_id": office["alice"].id,
            "amount": 45000,
            "source": "standalone_meeting",
            "delivery_address_id": office["alice_desk"].id,
            "notes": "collected at the office",
        },
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )

    assert response.status_code == 201, response.get_data(as_text=True)
    from business_app.models.payment import CashCollectionEvent

    event = db.session.get(
        CashCollectionEvent, response.get_json()["data"]["cash_collection_event"]["id"]
    )
    assert event.scope_type in ("personal", "cluster")
    assert _owed(office["bob"].id) == 20000.0
    assert _credit(office["alice"].id) == Decimal("20000.00")
