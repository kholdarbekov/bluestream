"""Instance #6 — a grocery account at a shared place: SHOWN == SETTLED.

THE DEFECT. ``CashCollectionService.resolve_allocation_scope`` FORCES personal
scope for a grocery account (``cash_collection_service.py`` layer-3 backstop):
its cash is mirrored onto a corporate contract and must never co-mingle with a
household's. Nothing on the DISPLAY side knew that, so both display halves
widened a grocery to its place anyway —
:meth:`StaffService.paginate_cod_debtors_for_staff` widened the row and
:func:`resolve_collect_scope` handed back a PLACE scope carrying the shop's
grouped address — while a real post settled only the shop's own debt.

MEASURED on the rows below (a shop owing 8 000 and an individual coworker owing
10 000 at the same plaza): every screen read **18 000**, the flow posted 18 000
against the shop's grouped address, the engine settled **8 000**, and the
coworker's 10 000 became the SHOP's prepaid credit. The receipt then said
"10 000 still collectible", the next lap offered that 10 000, settled *nothing*,
and pushed the credit to 20 000 — a debt every screen names and no lap can ever
pay.

THE RULING. The engine's refusal is deliberate and correct, so the ENGINE is
right and the DISPLAY is what must change. And it must not change by MIRRORING
the rule: a display-side ``user.is_grocery_store`` test would be a SECOND
expression of the engine's scope resolution, which is exactly the shape this
whole effort exists to delete — two expressions that agree today desynchronise
on the next edit. So the display ASKS
(:func:`business_app.services.cod_collect_ceiling.place_widening_applies` →
``resolve_allocation_scope``, under the same ``STANDALONE_MEETING`` source the
collect flows post with) and widens only when the answer is PLACE.

``test_the_gate_asks_the_engine_rather_than_mirroring_its_rules`` is the test
that distinguishes those two implementations, and it is the point of this file:
it moves the ENGINE's answer for an ordinary individual and requires every
display half to move with it. A mirror of ``is_grocery_store`` passes every
other test here and fails that one.

WHY REAL ROWS AND A REAL POST. The defect was a display expression and an engine
expression that agreed on every shape anyone had written a fixture for. So every
number below comes from real ``users`` / ``addresses`` / ``orders`` /
``payments`` rows through the REAL composition on the display side, and the
settlement half is measured by posting through the REAL engine and reading the
debt that moved — never by re-asserting arithmetic.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.cod_collect_ceiling import place_widening_applies
from business_app.services.staff_service import StaffService
from shared import business_config
from shared.enums import CashCollectionSource
from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler
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
    """Both halves of the place gate ON, and the Flask mirror restored after —
    ``app`` is session-scoped (tests/conftest.py), so a bare assignment would
    leak into every later test on the same xdist worker."""
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    app.config["PLACE_COD_COLLECTION_ENABLED"] = True
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


@pytest.fixture
def plaza(db):
    """A grocery and an individual sharing ONE grouped place.

        mart (GROCERY)  8 000 at the shop      ┐
        alice           10 000 at her office   ┘ both in place "plaza" = 18 000

    The contrast is the fixture: the engine's refusal is per-ACCOUNT, not
    per-place, so a blanket "no widening at a place with a grocery in it" would
    pass the grocery half and silently break the coworker.
    """
    mart = make_user(db, grocery=True)
    alice = make_user(db)
    admin = make_user(db)
    shop = make_address(db, mart)
    office = make_address(db, alice)
    group = make_place_group(db, shop, office, label="plaza")
    delivered_cod_order(db, mart, address=shop, total=Decimal("8000.00"))
    delivered_cod_order(db, alice, address=office, total=Decimal("10000.00"))
    return {
        "mart": mart,
        "alice": alice,
        "admin": admin,
        "group": group,
        "shop": shop,
        "office": office,
    }


def _row(user_id):
    """The person row the driver's debtor list actually renders."""
    result = StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)
    for row in result["items"]:
        if row.get("row_type") != "person":
            continue
        if user_id in (row.get("member_user_ids") or [row["id"]]):
            return row
    return None


def _offer(user_id):
    """``(delivery_address_id_to_post, amount)`` — the ONE call that decides both
    halves of the driver's offer, exactly as the handler does it."""
    statement = StaffService().get_customer_cod_statement_for_staff(user_id)
    return CashCollectionHandler._collect_offer(statement)


def _place_entry(user_id):
    statement = StaffService().get_customer_cod_statement_for_staff(user_id)
    places = statement.get("places") or []
    assert len(places) == 1, f"expected exactly one grouped place, got {places}"
    return places[0]


def _outstanding(service, user_id) -> Decimal:
    return Decimal(
        str(service.get_customer_cod_statement(user_id)["total_outstanding_amount"])
    )


# ---------------------------------------------------------------------------
# 1. THE DISPLAY — a grocery is shown and offered only its own debt
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_grocery_row_is_not_widened_to_the_place(app, db, plaza):
    """The debtor row. Before the fix this read 18 000."""
    row = _row(plaza["mart"].id)
    assert row is not None, "a grocery with its own open COD debt is still a debtor"
    assert Decimal(str(row["total_outstanding_amount"])) == Decimal("8000.00")
    assert row["active_cod_debt_count"] == 1


@pytest.mark.unit
def test_the_grocery_statement_publishes_no_place_ceiling(app, db, plaza):
    """The payload. Publishing NOTHING is how this payload already spells
    "degrade": both consumers — ``_scoped_ceiling`` and ``resolve_collect_scope``
    — read an absent ceiling as "cluster-scoped, no address", so the figure and
    the address drop TOGETHER. The fix needed no new key and no new branch."""
    place = _place_entry(plaza["mart"].id)
    assert "place_collect_ceiling_amount" not in place
    assert "place_collect_ceiling_debt_count" not in place
    # The raw engine keys are untouched — the place still HAS 18 000 of debt.
    # It is simply not this account's to collect.
    assert Decimal(str(place["place_open_cod_debt_total"])) == Decimal("18000.00")


@pytest.mark.unit
def test_the_grocery_offer_drops_the_address_together_with_the_figure(app, db, plaza):
    """The staff-bot offer. An address returned WITHOUT the place figure is the
    P0-degraded defect (a cluster-sized ceiling over a place-scoped post), so
    both halves are asserted."""
    address_id, amount = _offer(plaza["mart"].id)
    assert address_id is None
    assert Decimal(str(amount)) == Decimal("8000.00")


@pytest.mark.unit
def test_the_admin_collect_scope_for_a_grocery_carries_no_place(app, db, plaza):
    """The admin modal reads ONE resolved object and posts what it displays."""
    scope = StaffService().get_customer_cod_statement_for_admin(
        plaza["mart"].id
    )["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None
    assert Decimal(str(scope["amount"])) == Decimal("8000.00")
    assert scope["debt_count"] == 1


@pytest.mark.unit
def test_the_admin_dropdown_row_matches_that_scope(app, db, plaza):
    """``search_customers_for_cod_collection`` is the row an admin PICKS from;
    it must advertise the same figure the modal then collects."""
    rows = StaffService.search_customers_for_cod_collection(
        plaza["mart"].phone, "phone"
    )
    mine = [r for r in rows if r["id"] == plaza["mart"].id]
    assert len(mine) == 1
    assert Decimal(str(mine[0]["total_outstanding_amount"])) == Decimal("8000.00")


# ---------------------------------------------------------------------------
# 2. THE COWORKER — the refusal is per-ACCOUNT, not per-place
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_individual_coworker_at_the_same_place_is_unaffected(app, db, plaza):
    """Alice shares the plaza with a grocery and still gets the whole union.

    This is the test a blanket "no widening at a place with a grocery in it"
    fails: the engine grants HER a place scope, so every display half must too.
    """
    row = _row(plaza["alice"].id)
    assert row is not None
    assert Decimal(str(row["total_outstanding_amount"])) == Decimal("18000.00")
    assert row["active_cod_debt_count"] == 2

    place = _place_entry(plaza["alice"].id)
    assert Decimal(str(place["place_collect_ceiling_amount"])) == Decimal("18000.00")
    assert place["place_collect_ceiling_debt_count"] == 2

    address_id, amount = _offer(plaza["alice"].id)
    assert address_id == plaza["office"].id
    assert Decimal(str(amount)) == Decimal("18000.00")

    scope = StaffService().get_customer_cod_statement_for_admin(
        plaza["alice"].id
    )["collect_scope"]
    assert scope["scope_type"] == "place"
    assert scope["delivery_address_id"] == plaza["office"].id
    assert Decimal(str(scope["amount"])) == Decimal("18000.00")


# ---------------------------------------------------------------------------
# 3. THE SETTLEMENT — the offered figure is the debt that moves
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_posting_the_grocery_offer_settles_exactly_it_and_mints_no_credit(
    app, db, plaza
):
    """🔴 THE CLAUSE WITH TEETH. Post what the screens offer, through the REAL
    engine, and measure the debt that moved.

    Under the defect: 18 000 collected, 8 000 settled, 10 000 parked as MART's
    prepaid credit, Alice's debt untouched and re-offered on the next lap.
    """
    service = CashCollectionService()
    address_id, amount = _offer(plaza["mart"].id)

    event = service.post_collection(
        customer_id=plaza["mart"].id,
        amount=Decimal(str(amount)),
        source=CashCollectionSource.STANDALONE_MEETING,
        recorded_by_user_id=plaza["admin"].id,
        delivery_address_id=address_id,
        notes="the shop paid what the screen offered",
    )
    db.session.refresh(event)

    assert event.scope_type == "personal"
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert Decimal(str(service.get_customer_prepaid_balance(plaza["mart"].id))) == Decimal("0.00")
    # The shop's own debt is gone ...
    assert _outstanding(service, plaza["mart"].id) == Decimal("0.00")
    # ... the coworker's was never named, never advertised and never charged ...
    assert _outstanding(service, plaza["alice"].id) == Decimal("10000.00")
    # ... and the loop closes: nothing is offered to the shop a second time.
    assert _offer(plaza["mart"].id) == (None, 0.0)
    assert _row(plaza["mart"].id) is None


@pytest.mark.unit
def test_the_coworker_can_still_collect_the_whole_place(app, db, plaza):
    """The capability the fix must NOT cost: the plaza's 18 000 is still
    collectible — through the individual, whom the engine grants a place."""
    service = CashCollectionService()
    address_id, amount = _offer(plaza["alice"].id)

    event = service.post_collection(
        customer_id=plaza["alice"].id,
        amount=Decimal(str(amount)),
        source=CashCollectionSource.STANDALONE_MEETING,
        recorded_by_user_id=plaza["admin"].id,
        delivery_address_id=address_id,
        notes="the coworker paid for the plaza",
    )
    db.session.refresh(event)

    assert event.scope_type == "place"
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert _outstanding(service, plaza["alice"].id) == Decimal("0.00")
    assert _outstanding(service, plaza["mart"].id) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 4. THE SYNTHESISED ROW — half 2 of the debtor list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_debt_free_grocery_is_not_offered_a_coworkers_debt(app, db):
    """A grocery that owes nothing personally settles nothing at all, so a row
    for it would be a doorway to a settlement the engine will not perform.

    The individual in the same position DOES get one (owner rule 3: the office's
    debt is every coworker's), which is why both are asserted here — the fix must
    drop exactly one of these rows.
    """
    mart = make_user(db, grocery=True)
    bob = make_user(db)
    alice = make_user(db)
    shop, bob_desk, office = (
        make_address(db, mart),
        make_address(db, bob),
        make_address(db, alice),
    )
    make_place_group(db, shop, bob_desk, office, label="plaza")
    delivered_cod_order(db, alice, address=office, total=Decimal("10000.00"))

    assert _row(mart.id) is None, "a forced-personal account settles nothing here"
    bob_row = _row(bob.id)
    assert bob_row is not None, "owner rule 3: the office's debt is Bob's too"
    assert Decimal(str(bob_row["total_outstanding_amount"])) == Decimal("10000.00")


# ---------------------------------------------------------------------------
# 5. 🔴 ONE DECISION — the gate ASKS the engine, it does not restate its rules
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_gate_asks_the_engine_rather_than_mirroring_its_rules(
    app, db, plaza, monkeypatch
):
    """THE TEST THAT SEPARATES THE TWO IMPLEMENTATIONS.

    Move the ENGINE's answer for an ordinary INDIVIDUAL — no grocery flag
    anywhere near it — and every display half must move with it. A display that
    re-implemented the rule as ``if user.is_grocery_store`` passes every other
    test in this file and fails this one, which is precisely the desynchronised
    pair the whole effort exists to delete.
    """
    from business_app.services.allocation_scope import AllocationScope

    real = CashCollectionService.resolve_allocation_scope

    def _refuse_alice(self, customer_id, delivery_address_id=None, source=None):
        if int(customer_id) == plaza["alice"].id:
            return AllocationScope.personal(customer_id)
        return real(self, customer_id, delivery_address_id, source)

    monkeypatch.setattr(
        CashCollectionService, "resolve_allocation_scope", _refuse_alice
    )

    assert Decimal(str(_row(plaza["alice"].id)["total_outstanding_amount"])) == Decimal("10000.00")
    assert "place_collect_ceiling_amount" not in _place_entry(plaza["alice"].id)
    assert _offer(plaza["alice"].id) == (None, 10000.0)
    scope = StaffService().get_customer_cod_statement_for_admin(
        plaza["alice"].id
    )["collect_scope"]
    assert scope["scope_type"] == "cluster"
    assert scope["delivery_address_id"] is None


@pytest.mark.unit
def test_the_gate_asks_under_the_source_the_collect_flows_post_with(app, db, plaza):
    """``STANDALONE_MEETING`` is the only member of ``_PLACE_SCOPE_SOURCES`` a
    standalone collection uses, so it is the question the engine will itself
    answer at post time. Asking under a non-place source would refuse everybody
    and quietly disable widening catalogue-wide."""
    service = CashCollectionService()
    assert place_widening_applies(service, plaza["alice"].id, plaza["office"].id) is True
    assert place_widening_applies(service, plaza["mart"].id, plaza["shop"].id) is False
    assert (
        service.resolve_allocation_scope(
            plaza["alice"].id, plaza["office"].id, CashCollectionSource.STANDALONE_MEETING
        ).scope_type
        == "place"
    )


@pytest.mark.unit
def test_the_gate_refuses_every_other_reason_the_engine_declines_a_place(app, db):
    """Asking rather than mirroring refuses, for free, the reasons that have
    nothing to do with groceries — and will keep tracking the engine if a fifth
    is ever added there."""
    service = CashCollectionService()
    solo = make_user(db)
    ungrouped = make_address(db, solo)
    delivered_cod_order(db, solo, address=ungrouped, total=Decimal("5000.00"))

    assert place_widening_applies(service, solo.id, ungrouped.id) is False
    # No address at all is not a place either — and must not raise.
    assert place_widening_applies(service, solo.id, None) is False
