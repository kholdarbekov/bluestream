"""Owner ruling A6 — the debtor ROW and the collect CEILING are ONE calculation.

A6, verbatim: *"In debtors list Alice MUST show 45K and 'collect all' should also
offer that exact amount 45K. They should use same calculation."*

WHAT BROKE. `StaffService.paginate_cod_debtors_for_staff` composed the row as a
UNION — the person's own cluster debt plus the share of their workplace's debt
that is not already theirs — while the staff bot capped the collection at
``max(total_outstanding, cluster_total, place_total)``. On the canonical numbers
that is ``max(25k, 25k, 35k) = 35k`` against a row of 45 000: the list advertised
a figure the collect flow refused, breaking the invariant
`staff_service.py:2414` states in so many words, and the 10 000 in between was
settling a coworker's debt while the overpayment confirmation told the driver in
all three languages that it was becoming prepayment.

WHY THESE TESTS ARE BUILT FROM REAL ROWS. The defect was two expressions that
agreed on every shape anyone had written a fixture for. A test that feeds a
hand-written statement to the bot can only re-assert the fixture author's
arithmetic, so every number below comes from real ``users`` / ``addresses`` /
``orders`` / ``payments`` rows through the REAL composition on both sides — the
row through ``paginate_cod_debtors_for_staff``, the ceiling through
``get_customer_cod_statement_for_staff`` and the REAL handler classmethods.

THE CANONICAL SCENARIO (A6):
    Alice  10 000 at an ungrouped home  +  15 000 at office G
    Bob                                    20 000 at office G
    => place G owes 35 000; Alice's row and ceiling are 45 000, Bob's are 35 000.

OWNER RULING A7 (2026-08-05) then removed the 🏢 place row and its screen from
the staff bot altogether — *"the debtors list only shows the users, and the
office debt is included in each coworker's debt"* — which makes the person row
the ONLY doorway to the office's debt. Everything A6 pins therefore has to keep
holding; `test_the_office_is_collectible_only_through_a_person` asserts the new
half.
"""

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.staff_service import StaffService
from shared import business_config
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


class _Ctx:
    """Minimal stand-in for a telegram context.

    Since owner ruling A7 the handlers read nothing from ``user_data`` that can
    influence WHICH PLACE a collection posts against — the place screen that
    wrote ``pending_place_group_id`` is gone, and ``_resolved_place`` no longer
    takes a context at all."""

    def __init__(self):
        self.user_data = {"language": "en", "authenticated": True,
                          "staff_roles": ["delivery_driver"]}


@pytest.fixture(autouse=True)
def _gate(app, monkeypatch):
    """Both halves of the gate ON, and the Flask mirror restored afterwards —
    ``app`` is session-scoped (tests/conftest.py:113), so a bare assignment
    leaks into every later test on the same xdist worker."""
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    app.config["PLACE_COD_COLLECTION_ENABLED"] = True
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


@pytest.fixture
def office(db):
    """The A6 scenario, as rows."""
    alice, bob, admin = make_user(db), make_user(db), make_user(db)
    alice_home = make_address(db, alice)               # UNGROUPED
    alice_desk, bob_desk = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, alice_desk, bob_desk)
    delivered_cod_order(db, alice, address=alice_home, total=Decimal("10000.00"))
    delivered_cod_order(db, alice, address=alice_desk, total=Decimal("15000.00"))
    delivered_cod_order(db, bob, address=bob_desk, total=Decimal("20000.00"))
    return {"alice": alice, "bob": bob, "admin": admin, "group": group,
            "alice_home": alice_home, "alice_desk": alice_desk, "bob_desk": bob_desk}


def _row(user_id):
    """The person row the driver's debtor list actually renders."""
    result = StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)
    for row in result["items"]:
        if row.get("row_type") != "person":
            continue
        if user_id in (row.get("member_user_ids") or [row["id"]]):
            return row
    return None


def _scope_and_ceiling(user_id, statement=None):
    """The scope the collect flow will POST and the ceiling it will ENFORCE —
    from the one call that decides both, exactly as the handler does it.

    This calls the handler's own ``_collect_offer`` rather than re-composing
    ``_resolved_place`` → ``_scoped_ceiling`` here: a helper that reproduced the
    composition would keep passing if the handler stopped using it, which is the
    shape of every defect this file pins.
    """
    if statement is None:
        statement = StaffService().get_customer_cod_statement_for_staff(user_id)
    return CashCollectionHandler._collect_offer(statement)


def _rendered_statement(user_id, statement=None, language="en"):
    """The screen the driver actually reads, from the REAL formatter on the REAL
    served payload — never a hand-built string."""
    if statement is None:
        statement = StaffService().get_customer_cod_statement_for_staff(user_id)
    return CashCollectionHandler._format_statement(statement, language)


def _ceiling(user_id):
    """The ceiling half of that one decision."""
    return _scope_and_ceiling(user_id)[1]


def _strip_published_ceilings(statement):
    """The payload a business_app OLDER than this bot serves: the raw engine
    statement, carrying no ``place_collect_ceiling_*`` keys at all. Exactly what
    `get_customer_cod_statement_for_staff` returns before the P0 fix, and what it
    still returns with the gate off."""
    for place in statement.get("places") or []:
        place.pop("place_collect_ceiling_amount", None)
        place.pop("place_collect_ceiling_debt_count", None)
    return statement


def _collect_full(monkeypatch, user_id, statement=None):
    """Drive the REAL ``start_full_collection`` over the REAL served statement."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    if statement is None:
        statement = StaffService().get_customer_cod_statement_for_staff(user_id)

    class _AsyncClient:
        def __init__(self):
            self.client = MagicMock()
            self.client.get_customer_cod_statement = AsyncMock(
                return_value=MagicMock(success=True, data=statement)
            )

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_):
            return False

    handler = CashCollectionHandler()
    update = MagicMock()
    update.effective_user = MagicMock(id=999)
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = f"staff_cod_collect_full_{user_id}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = _Ctx()
    context.bot = MagicMock()

    monkeypatch.setattr(mod, "api_client", _AsyncClient())
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.start_full_collection(update, context))
    return context.user_data.get("pending_cod_collection_flow")


def _collect_custom(monkeypatch, user_id, typed_amount, statement=None):
    """Drive the WHOLE custom flow through the REAL handlers and return
    ``(posted_payload, overpayment_shown)``.

    ``start_custom_collection`` → ``receive_collection_amount`` (types the
    amount) → ``confirm_overpayment_collection`` if the surplus prompt fires →
    ``receive_collection_note``. The payload captured at the end is byte-for-byte
    what ``POST /api/v1/staff/cash-collections`` would receive, so it can be
    replayed through the real engine and the money measured.
    """
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    if statement is None:
        statement = StaffService().get_customer_cod_statement_for_staff(user_id)
    posted = {}

    async def _record(_token, payload):
        posted.update(payload)
        return MagicMock(success=True, data={"cash_collection_event": {"id": 1}})

    class _AsyncClient:
        def __init__(self):
            self.client = MagicMock()
            self.client.get_customer_cod_statement = AsyncMock(
                return_value=MagicMock(success=True, data=statement)
            )
            self.client.record_cash_collection = AsyncMock(side_effect=_record)

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_):
            return False

    handler = CashCollectionHandler()
    context = _Ctx()
    context.bot = MagicMock()
    monkeypatch.setattr(mod, "api_client", _AsyncClient())
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    def _update(text=None, data=None):
        upd = MagicMock()
        upd.effective_user = MagicMock(id=999)
        if text is None:
            upd.message = None
        else:
            upd.message = MagicMock()
            upd.message.text = text
            upd.message.reply_text = AsyncMock()
        if data is None:
            upd.callback_query = None
        else:
            upd.callback_query = MagicMock()
            upd.callback_query.data = data
            upd.callback_query.answer = AsyncMock()
            upd.callback_query.edit_message_text = AsyncMock()
        return upd

    asyncio.run(handler.start_custom_collection(
        _update(data=f"staff_cod_collect_custom_{user_id}"), context))
    state = asyncio.run(handler.receive_collection_amount(
        _update(text=str(typed_amount)), context))
    overpayment_shown = state == mod.COLLECTION_OVERPAYMENT_CONFIRM
    if overpayment_shown:
        asyncio.run(handler.confirm_overpayment_collection(
            _update(data="staff_cod_confirm_overpay_yes"), context))
    asyncio.run(handler.receive_collection_note(_update(text="at the office"), context))
    return posted, overpayment_shown


# ---------------------------------------------------------------------------
# 🔴 THE PIN — the row and the ceiling are the same number
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_alices_row_and_ceiling_are_both_45000(app, db, office):
    """A6's headline number, from both ends."""
    row = _row(office["alice"].id)
    assert row is not None
    assert row["total_outstanding_amount"] == 45000.0
    assert row["active_cod_debt_count"] == 3          # her two + Bob's one
    assert _ceiling(office["alice"].id) == 45000.0


@pytest.mark.unit
def test_bobs_row_and_ceiling_are_both_35000(app, db, office):
    """Same rule both ways — the owner ruled Bob 35 000 explicitly. Note this is
    the number the OLD `max` happened to get right for Bob and wrong for Alice,
    which is why both people are pinned."""
    row = _row(office["bob"].id)
    assert row is not None
    assert row["total_outstanding_amount"] == 35000.0
    assert row["active_cod_debt_count"] == 2
    assert _ceiling(office["bob"].id) == 35000.0


@pytest.mark.unit
def test_every_person_row_equals_its_own_collect_ceiling(app, db, office):
    """🔴 THE INVARIANT `staff_service.py:2414` STATES: "never advertise a total
    the collect flow refuses". Do not delete this test.

    The two previous tests pin the two numbers A6 names. This one pins the
    RELATION for every row on the list, so a future edit to either side that
    keeps Alice at 45 000 while changing anyone else still fails here.
    """
    rows = [r for r in StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)["items"]
            if r.get("row_type") == "person"]
    assert rows, "the scenario must produce person rows or this test asserts nothing"
    for row in rows:
        assert _ceiling(row["id"]) == row["total_outstanding_amount"], (
            f"row {row['id']} advertises {row['total_outstanding_amount']} "
            f"but the collect flow caps at {_ceiling(row['id'])}"
        )


@pytest.mark.unit
def test_the_published_ceiling_carries_the_matching_debt_count(app, db, office):
    """The count travels with the amount, so a surface can state "3 debts,
    45 000" without recomputing either half."""
    statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    place = next(p for p in statement["places"]
                 if p["place_group_id"] == office["group"].id)
    assert place["place_collect_ceiling_amount"] == 45000.0
    assert place["place_collect_ceiling_debt_count"] == 3
    # The place's OWN total is untouched in the payload — it is what the admin
    # place view and the at-door prompt read. The staff bot no longer renders a
    # 🏢 row from it (A7).
    assert place["place_open_cod_debt_total"] == 35000.0


# ---------------------------------------------------------------------------
# End to end: what "Collect all" actually offers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_collect_all_offers_alice_exactly_her_row(app, db, monkeypatch, office):
    """R-B end to end. Under the shipped `max` this flow offered 35 000."""
    flow = _collect_full(monkeypatch, office["alice"].id)
    assert flow["amount"] == 45000.0
    assert flow["delivery_address_id"] == office["alice_desk"].id


@pytest.mark.unit
def test_collect_all_offers_bob_exactly_his_row(app, db, monkeypatch, office):
    flow = _collect_full(monkeypatch, office["bob"].id)
    assert flow["amount"] == 35000.0
    assert flow["delivery_address_id"] == office["bob_desk"].id


@pytest.mark.unit
def test_a_debt_free_coworkers_row_and_ceiling_agree(app, db, monkeypatch, office):
    """Carol owes nothing of her own, so her row is SYNTHESISED
    (`staff_service.py:_synthesise_debt_free_place_member_rows`) out of the
    office's debt alone. The synthesised row and the ceiling must agree too —
    that row exists only to be tapped and collected from."""
    carol = make_user(db)
    make_place_group(db, make_address(db, carol), label="office")
    carol_desk = carol.addresses[0]
    carol_desk.address_group_id = office["group"].id
    db.session.commit()

    row = _row(carol.id)
    assert row is not None, "a debt-free coworker at an indebted place must be reachable"
    assert row["total_outstanding_amount"] == 35000.0
    assert _ceiling(carol.id) == 35000.0
    assert _collect_full(monkeypatch, carol.id)["amount"] == 35000.0


# ---------------------------------------------------------------------------
# The rollback path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_gate_off_leaves_the_row_and_the_ceiling_un_widened(app, db, monkeypatch, office):
    """C0. With the gate off nothing widens on EITHER side: the row is the
    engine's own, the statement carries no ceiling field at all, and the flow
    offers the person's own cluster debt — Plan D behaviour, byte for byte."""
    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    row = _row(office["alice"].id)
    assert row["total_outstanding_amount"] == 25000.0          # her own two debts

    statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    assert all("place_collect_ceiling_amount" not in p for p in statement["places"])
    assert _ceiling(office["alice"].id) == 25000.0
    assert _collect_full(monkeypatch, office["alice"].id)["amount"] == 25000.0


@pytest.mark.unit
def test_the_ceiling_is_delivered_only_on_both_steps(app, db, office):
    """The second desynchronisation, folded in. ``start_full_collection`` summed
    DELIVERED items while ``receive_collection_amount`` used the per-account
    ``total_outstanding_amount``, which counts PENDING orders too. The engine's
    candidate rings select DELIVERED orders only
    (``cash_collection_service.py:183-196`` / ``:245-259``), so cash offered
    against a pending order settles nothing and silently becomes prepayment.
    One base, delivered-only, for both steps.
    """
    from shared.enums import OrderStatus

    delivered_cod_order(db, office["alice"], address=office["alice_home"],
                        total=Decimal("70000.00"), status=OrderStatus.PENDING)

    statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    # The engine's per-account headline DOES count the pending order...
    assert statement["total_outstanding_amount"] == 95000.0
    # ...and the ceiling does not.
    assert _ceiling(office["alice"].id) == 45000.0
    assert _row(office["alice"].id)["total_outstanding_amount"] == 45000.0


# ---------------------------------------------------------------------------
# 🔴 Where the money lands — the claim the driver is shown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_surplus_above_the_ceiling_becomes_the_collecting_persons_prepayment(app, db, office):
    """🔴 A6/R-D — the shipped overpayment copy, verified. Do not delete.

    ``staff.delivery.cod_collection_overpayment_confirm``
    (scripts/seed_staff_translations.py:1483-1487) promises the driver, in all
    three languages, that the surplus "will be recorded as customer prepayment".
    That is only true if the threshold it is measured against is the WHOLE
    settlement set. Collect 50 000 against a 45 000 ceiling: every debt in
    ring 1 ∪ ring 2 must be gone — Alice's home debt, Alice's office debt AND
    BOB'S office debt — and exactly the 5 000 above the ceiling may remain, as
    ALICE's credit and nobody else's (R-D: "surplus prepaid credit is per-user").

    Under the shipped 35 000 ceiling the same copy fired at 40 000, where the
    5 000 "surplus" was still paying Bob's debt down.
    """
    service = CashCollectionService()
    event = service.post_collection(
        customer_id=office["alice"].id,
        amount=Decimal("50000.00"),
        source="standalone_meeting",
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=office["alice_desk"].id,
        notes="collected at the office",
    )

    db.session.refresh(event)
    assert event.scope_type == "place"
    # Nothing left in either ring — the ceiling really was the whole set.
    assert service.get_customer_cod_statement(office["alice"].id)["total_outstanding_amount"] == 0.0
    assert service.get_customer_cod_statement(office["bob"].id)["total_outstanding_amount"] == 0.0
    assert float(service.get_place_open_cod_debt_total(office["group"].id)) == 0.0
    # ...and only the surplus survives, on the person who paid it.
    assert Decimal(str(event.unapplied_amount)) == Decimal("5000.00")
    assert Decimal(str(service.get_customer_prepaid_balance(office["alice"].id))) == Decimal("5000.00")
    assert Decimal(str(service.get_customer_prepaid_balance(office["bob"].id))) == Decimal("0.00")


@pytest.mark.unit
def test_collecting_exactly_the_ceiling_leaves_no_surplus_at_all(app, db, office):
    """The other side of the same claim: at the ceiling the driver must see no
    overpayment prompt, because there is nothing left over to explain."""
    service = CashCollectionService()
    event = service.post_collection(
        customer_id=office["alice"].id,
        amount=Decimal("45000.00"),
        source="standalone_meeting",
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=office["alice_desk"].id,
        notes="collected at the office",
    )

    db.session.refresh(event)
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert Decimal(str(service.get_customer_prepaid_balance(office["alice"].id))) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 🔴 THE DEGRADED PATH — the ceiling and the posting scope are ONE decision
# ---------------------------------------------------------------------------
#
# P0 shipped a fallback: when the payload carries no `place_collect_ceiling_amount`
# the bot offered the CLUSTER-ONLY figure — but it still stored and posted
# `delivery_address_id`, so the settlement stayed PLACE-scoped and still cleared
# ring 1 ∪ ring 2. Measured on the canonical rows: ceiling 25 000, driver posts
# 45 000, the confirmation promises 20 000 of prepayment, `unapplied_amount` is
# 0.00 and Bob's debt is gone. ZERO of the promised surplus existed. The comment
# above the branch called it "under-offering, the safe direction" — it is not:
# a ceiling BELOW the settlement set is precisely what makes the copy false.
#
# These tests measure the SETTLEMENT, not the ceiling. A test that asserted only
# `ceiling == 25000` passed throughout the defect.


def _promised_surplus(typed, ceiling):
    """What `staff.delivery.cod_collection_overpayment_confirm` tells the driver
    will become prepayment (seed_staff_translations.py:1483-1487)."""
    return Decimal(str(typed)) - Decimal(str(ceiling))


@pytest.mark.unit
def test_degraded_ceiling_forces_cluster_scope_so_the_promised_surplus_is_real(
    app, db, monkeypatch, office
):
    """🔴 THE P0-DEGRADED PIN. Do not delete, and do not weaken it to a ceiling
    assertion — a ceiling assertion is exactly what missed this.

    A `staff_bot` newer than its `business_app` (the documented deploy-skew
    window) gets a statement with no published ceiling. The offer must then be
    the cluster-only figure AND the post must be cluster-scoped, so that the
    surplus the driver is promised is the surplus that actually exists.
    """
    service = CashCollectionService()
    statement = _strip_published_ceilings(
        StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    )

    flow = _collect_full(monkeypatch, office["alice"].id, statement=statement)

    # The offer degrades to Alice's own cluster debt...
    assert flow["amount"] == 25000.0
    # ...and — the whole point — the SCOPE degrades with it.
    assert flow["delivery_address_id"] is None, (
        "a cluster-only ceiling paired with a place-scoped post is the defect: "
        "the post settles ring 1 ∪ ring 2 (45 000) under a 25 000 ceiling"
    )

    # Now measure the money the driver was promised. She over-types the row
    # figure; the confirmation promises 45 000 - 25 000 = 20 000 of prepayment.
    typed = Decimal("45000.00")
    promised = _promised_surplus(typed, flow["amount"])
    assert promised == Decimal("20000.00")

    event = service.post_collection(
        customer_id=office["alice"].id,
        amount=typed,
        source="standalone_meeting",
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=flow["delivery_address_id"],
        notes="deploy-skew collection",
    )
    db.session.refresh(event)

    assert event.scope_type != "place"
    # The promise is kept, to the cent. Under the defect this was 0.00.
    assert Decimal(str(event.unapplied_amount)) == promised
    assert Decimal(str(service.get_customer_prepaid_balance(office["alice"].id))) == promised
    # Alice's own two debts are settled — the cluster ring, and nothing beyond it.
    assert service.get_customer_cod_statement(office["alice"].id)["total_outstanding_amount"] == 0.0
    # 🔴 NO COWORKER'S DEBT MOVED. Bob was never named, never selected, and his
    # money was never offered to the driver.
    assert service.get_customer_cod_statement(office["bob"].id)["total_outstanding_amount"] == 20000.0
    assert float(service.get_place_open_cod_debt_total(office["group"].id)) == 20000.0
    assert Decimal(str(service.get_customer_prepaid_balance(office["bob"].id))) == Decimal("0.00")


@pytest.mark.unit
def test_degraded_custom_flow_posts_cluster_scoped_end_to_end(app, db, monkeypatch, office):
    """The same rule down the CUSTOM path, which decides scope and ceiling in two
    different Telegram updates — `start_custom_collection` stored the address and
    `receive_collection_amount` priced it, so the split lived across the two.

    Drives the real handlers all the way to the posted payload, then replays that
    payload through the real engine exactly as `api/staff.py:616-632` does.
    """
    service = CashCollectionService()
    statement = _strip_published_ceilings(
        StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    )

    posted, overpayment_shown = _collect_custom(
        monkeypatch, office["alice"].id, 45000, statement=statement
    )

    assert overpayment_shown, "45 000 over a 25 000 ceiling must show the surplus copy"
    assert posted["delivery_address_id"] is None
    assert posted["amount"] == 45000.0

    event = service.post_collection(
        customer_id=posted["customer_id"],
        amount=Decimal(str(posted["amount"])),
        source=posted["source"],
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=posted["delivery_address_id"],
        notes=posted["notes"],
    )
    db.session.refresh(event)

    assert event.scope_type != "place"
    assert Decimal(str(event.unapplied_amount)) == Decimal("20000.00")
    assert Decimal(str(service.get_customer_prepaid_balance(office["alice"].id))) == Decimal("20000.00")
    assert service.get_customer_cod_statement(office["bob"].id)["total_outstanding_amount"] == 20000.0


@pytest.mark.unit
def test_the_custom_flow_re_decides_scope_when_the_ceiling_disappears_mid_flow(
    app, db, monkeypatch, office
):
    """The two custom-flow updates are minutes apart. If the ceiling is published
    when the driver taps Collect and gone when they type the amount — a backend
    rollback, or the gate flipped off mid-flow — the address stored at step 1
    must be dropped at step 2, because step 2 is where the copy is shown."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    fresh = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    stale_free = _strip_published_ceilings(
        StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    )
    served = [fresh, stale_free, stale_free]
    posted = {}

    async def _record(_token, payload):
        posted.update(payload)
        return MagicMock(success=True, data={"cash_collection_event": {"id": 1}})

    class _AsyncClient:
        def __init__(self):
            self.client = MagicMock()
            self.client.get_customer_cod_statement = AsyncMock(
                side_effect=lambda *_a, **_k: MagicMock(success=True, data=served.pop(0))
            )
            self.client.record_cash_collection = AsyncMock(side_effect=_record)

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_):
            return False

    handler = CashCollectionHandler()
    context = _Ctx()
    context.bot = MagicMock()
    monkeypatch.setattr(mod, "api_client", _AsyncClient())
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    def _update(text=None, data=None):
        upd = MagicMock()
        upd.effective_user = MagicMock(id=999)
        upd.message = None
        upd.callback_query = None
        if text is not None:
            upd.message = MagicMock()
            upd.message.text = text
            upd.message.reply_text = AsyncMock()
        if data is not None:
            upd.callback_query = MagicMock()
            upd.callback_query.data = data
            upd.callback_query.answer = AsyncMock()
            upd.callback_query.edit_message_text = AsyncMock()
        return upd

    asyncio.run(handler.start_custom_collection(
        _update(data=f"staff_cod_collect_custom_{office['alice'].id}"), context))
    # Step 1 saw a published ceiling, so it legitimately committed to the place.
    assert context.user_data["pending_cod_collection_flow"]["delivery_address_id"] == \
        office["alice_desk"].id

    state = asyncio.run(handler.receive_collection_amount(_update(text="45000"), context))
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM
    flow = context.user_data["pending_cod_collection_flow"]
    # Step 2 priced 25 000, so step 2 must also un-commit the place.
    assert flow["total_outstanding_amount"] == 25000.0
    assert flow["delivery_address_id"] is None

    asyncio.run(handler.confirm_overpayment_collection(
        _update(data="staff_cod_confirm_overpay_yes"), context))
    asyncio.run(handler.receive_collection_note(_update(text="note"), context))
    assert posted["delivery_address_id"] is None


@pytest.mark.unit
def test_an_unparseable_published_ceiling_also_drops_the_place(app, db, monkeypatch, office):
    """Every degradation drops the address, not just the missing-key one — a
    malformed ceiling is the same information (we do not know the settlement
    set) and must not be paired with a place-scoped post."""
    statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    for place in statement["places"]:
        place["place_collect_ceiling_amount"] = "not-a-number"

    assert _scope_and_ceiling(office["alice"].id, statement=statement) == (None, 25000.0)
    flow = _collect_full(monkeypatch, office["alice"].id, statement=statement)
    assert flow["delivery_address_id"] is None
    assert flow["amount"] == 25000.0


@pytest.mark.unit
def test_gate_off_posts_no_place_address_at_all(app, db, monkeypatch, office):
    """C0, on the money side. Plan D posted no `delivery_address_id` — the key
    does not exist anywhere in this file at HEAD — so a rollback that still posts
    one is not a rollback. With the gate off the backend publishes no ceiling
    either, which is the same degradation, and it resolves the same way."""
    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    scope, ceiling = _scope_and_ceiling(office["alice"].id)
    assert (scope, ceiling) == (None, 25000.0)

    posted, _ = _collect_custom(monkeypatch, office["alice"].id, 25000)
    assert posted["delivery_address_id"] is None

    event = CashCollectionService().post_collection(
        customer_id=posted["customer_id"],
        amount=Decimal(str(posted["amount"])),
        source=posted["source"],
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=posted["delivery_address_id"],
        notes=posted["notes"],
    )
    db.session.refresh(event)
    assert event.scope_type != "place"
    # Bob is untouched: gate off means the coworker ring is not reachable.
    assert CashCollectionService().get_customer_cod_statement(
        office["bob"].id)["total_outstanding_amount"] == 20000.0


# ---------------------------------------------------------------------------
# 🔴 OWNER RULING A7 — the office is collectible ONLY through a person
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_staff_debtor_list_shows_no_place_row(app, db, office):
    """🔴 A7/R-E. Do not delete this test.

    *"in staff bot there won't be any 'office' row in debtors list."* Measured
    against the engine, which still HAS that row — so an empty result here is
    the staff composition dropping it, not a fixture with no grouped place.
    """
    engine_rows = CashCollectionService().paginate_users_with_open_cod_debts(
        page=1, per_page=100
    )["items"]
    assert [r for r in engine_rows if r.get("row_type") == "place"], (
        "the engine must still emit a place row, or this test proves nothing"
    )

    items = StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)["items"]
    assert items, "the A6 scenario must produce rows"
    assert all(r.get("row_type") == "person" for r in items)
    assert all(r.get("id") is not None for r in items), (
        "every row must be renderable by DeliveryKeyboards.cod_debtor_list"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "who, expected", [("alice", 45000.0), ("bob", 35000.0)]
)
def test_the_office_is_collectible_only_through_a_person(
    app, db, monkeypatch, office, who, expected
):
    """🔴 A7/R-F, END TO END, ON REAL ROWS. Do not delete this test.

    A7 removed the place doorway. This asserts the remaining one carries the
    whole load, for BOTH coworkers and in all three of the places the number has
    to agree:

      1. the debtor ROW the driver reads (A6/R-A);
      2. what "Collect full" OFFERS (A6/R-B — same calculation, not two that
         agree);
      3. what the money actually SETTLES when that offer is posted (A6/R-C) —
         Alice's own debts AND the office's, whichever of the two was tapped.

    Alice 45 000, Bob 35 000, and either one clears office G to zero.
    """
    user = office[who]
    row = _row(user.id)
    assert row is not None, "the coworker must have a tappable row"
    assert row["total_outstanding_amount"] == expected
    assert _collect_full(monkeypatch, user.id)["amount"] == expected

    service = CashCollectionService()
    event = service.post_collection(
        customer_id=user.id,
        amount=Decimal(str(expected)),
        source="standalone_meeting",
        recorded_by_user_id=office["admin"].id,
        delivery_address_id=_scope_and_ceiling(user.id)[0],
        notes="collected from a person, at the office",
    )
    db.session.refresh(event)

    assert event.scope_type == "place"
    # The office is settled either way — that is R-F's "through a person".
    assert float(service.get_place_open_cod_debt_total(office["group"].id)) == 0.0
    assert service.get_customer_cod_statement(office["bob"].id)["total_outstanding_amount"] == 0.0
    # Exactly the row figure, so nothing spills into prepayment.
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    # Bob's 35 000 does not reach Alice's PRIVATE home debt — that 10 000 is
    # hers alone and is precisely the delta that made the old place screen lie.
    alice_left = service.get_customer_cod_statement(office["alice"].id)["total_outstanding_amount"]
    assert alice_left == (0.0 if who == "alice" else 10000.0)


# ---------------------------------------------------------------------------
# 🔴 THE FIFTH INSTANCE — the SCREEN between the row and the offer
# ---------------------------------------------------------------------------
#
# A7 left exactly one doorway standing: the person COD statement screen. It
# rendered `statement['total_outstanding_amount']` (per-account, PENDING-
# inclusive) and `places[].place_open_cod_debt_total` straight off the raw engine
# payload, while "💸 Collect full" priced itself through `_scoped_ceiling`.
# On the canonical rows the screen read 25 000 / 🏢 35 000 and then offered
# 45 000 — a number that appeared NOWHERE on it, and the number the debtor list
# that got the driver there had advertised. One PENDING order widened the
# headline to 95 000 against the same 45 000.
#
# Measured, it did not misallocate money: the engine's rings are DELIVERED-only,
# so the surplus became the collecting person's own prepayment and no coworker
# was charged. It broke the invariant `staff_service.py:2414` states instead —
# "never advertise a total the collect flow refuses" — at the moment cash changes
# hands. These tests render the REAL formatter on the REAL served payload; a test
# that asserted a variable held the ceiling passed throughout the defect.


def _money_lines(text):
    """Every rendered line carrying a currency figure, in screen order."""
    return [line for line in text.splitlines() if "Uzs" in line]


@pytest.mark.unit
def test_the_statement_screen_shows_the_figure_collect_full_will_offer(
    app, db, monkeypatch, office
):
    """🔴 THE PIN. Do not delete, and do not weaken it to "some number is shown".

    Three surfaces, one number: the debtor ROW that got the driver here, the
    SCREEN they read, and what "Collect full" then OFFERS. Under the defect the
    screen was the odd one out.
    """
    alice = office["alice"].id
    screen = _rendered_statement(alice)
    offered = _collect_full(monkeypatch, alice)["amount"]

    assert offered == 45000.0
    assert _row(alice)["total_outstanding_amount"] == offered
    # THE assertion: the offer is on the screen the driver read.
    assert "45,000" in screen, screen

    # ...and it is the HEADLINE, not one component among several. The first
    # money line a driver's eye lands on must be the collectible figure.
    assert "45,000" in _money_lines(screen)[0], screen


@pytest.mark.unit
def test_the_screen_never_shows_the_raw_per_account_total_as_a_figure(
    app, db, office
):
    """The exact number the defect displayed. Alice's per-account
    ``total_outstanding_amount`` is 25 000 — her own two debts, excluding Bob's
    20 000 that the very same tap will settle. It headed the screen as "Total
    outstanding" under a 45 000 offer, so it must not survive anywhere on it."""
    statement = StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    assert statement["total_outstanding_amount"] == 25000.0, "fixture must reproduce A6"

    screen = CashCollectionHandler._format_statement(statement, "en")
    assert "25,000" not in screen, screen
    assert "Total outstanding" not in screen, screen


@pytest.mark.unit
def test_the_pending_order_shape_shows_the_collectible_figure_not_the_gross(
    app, db, monkeypatch, office
):
    """🔴 WHERE THE DIVERGENCE IS WIDEST — the shape the admin fix called out.

    A PENDING order is COD debt the allocation engine cannot touch (its candidate
    rings are DELIVERED-only, ``cash_collection_service.py:183-196`` / ``:245-259``),
    so it inflates ``total_outstanding_amount`` and nothing else. On the admin
    modal this displayed 95 000 against a 45 000 collection; the driver's screen
    served the same field and was never fixed.
    """
    from shared.enums import OrderStatus

    alice = office["alice"].id
    delivered_cod_order(db, office["alice"], address=office["alice_home"],
                        total=Decimal("70000.00"), status=OrderStatus.PENDING)

    statement = StaffService().get_customer_cod_statement_for_staff(alice)
    assert statement["total_outstanding_amount"] == 95000.0, "fixture must be PENDING-inflated"

    screen = CashCollectionHandler._format_statement(statement, "en")
    offered = _collect_full(monkeypatch, alice, statement=statement)["amount"]

    assert offered == 45000.0
    assert "45,000" in _money_lines(screen)[0], screen
    # The 50 000 lie, gone.
    assert "95,000" not in screen, screen


@pytest.mark.unit
def test_the_workplace_line_is_a_labelled_component_not_a_total(app, db, office):
    """``place_open_cod_debt_total`` stays on the screen — the driver standing in
    an office needs to know what that office owes — but it is 35 000 where the
    offer is 45 000, so it must read as a NAMED component.

    This is the same 10 000 delta that made owner ruling A7 delete the place
    screen outright: there it was the header, with no larger figure above it to
    subordinate it to.
    """
    screen = _rendered_statement(office["alice"].id)

    place_line = next(line for line in screen.splitlines() if line.startswith("🏢"))
    assert "35,000" in place_line
    # The workplace label alone ("🏢 office: 35,000") read as a headline. The
    # SSOT workplace-debt label must name what the figure IS, with the group's
    # own label demoted to a parenthetical.
    assert "Place cod total" in place_line, place_line   # humanised i18n fallback
    assert "(office)" in place_line, place_line
    # And it is below the collectible headline, never above it.
    assert screen.index("🏢") > screen.index("45,000")


@pytest.mark.unit
def test_bobs_screen_and_bobs_offer_agree_too(app, db, monkeypatch, office):
    """Both coworkers, because the defect's magnitude differed per person: Bob's
    raw per-account total (20 000) and the place total (35 000) straddled his
    35 000 offer, so a one-person assertion could have been satisfied by
    coincidence."""
    bob = office["bob"].id
    screen = _rendered_statement(bob)

    assert _collect_full(monkeypatch, bob)["amount"] == 35000.0
    assert "35,000" in _money_lines(screen)[0], screen
    assert "20,000" not in _money_lines(screen)[0], screen


@pytest.mark.unit
def test_the_screen_degrades_with_the_offer_when_no_ceiling_is_published(
    app, db, monkeypatch, office
):
    """The deploy-skew window, on the DISPLAY side.

    With no published ceiling the flow degrades to cluster scope and offers
    25 000 (``test_degraded_ceiling_forces_cluster_scope_...``). The screen must
    degrade with it — the old headline advertised the raw 25 000 by accident here
    and 95 000 the moment a pending order existed, which is the same split
    pointing the other way.
    """
    statement = _strip_published_ceilings(
        StaffService().get_customer_cod_statement_for_staff(office["alice"].id)
    )
    screen = CashCollectionHandler._format_statement(statement, "en")
    flow = _collect_full(monkeypatch, office["alice"].id, statement=statement)

    assert flow["amount"] == 25000.0
    assert flow["delivery_address_id"] is None
    assert "25,000" in _money_lines(screen)[0], screen
    assert "45,000" not in screen, screen


@pytest.mark.unit
def test_gate_off_screen_matches_the_gate_off_offer(app, db, monkeypatch, office):
    """C0 on the display side: with the gate off the flow offers Plan D's
    un-widened cluster figure, so the screen must state that and not the
    workplace's."""
    app.config["PLACE_COD_COLLECTION_ENABLED"] = False
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)

    screen = _rendered_statement(office["alice"].id)
    assert _collect_full(monkeypatch, office["alice"].id)["amount"] == 25000.0
    assert "25,000" in _money_lines(screen)[0], screen
    assert "45,000" not in screen, screen


@pytest.mark.unit
def test_every_person_screen_states_that_persons_own_offer(app, db, monkeypatch, office):
    """The RELATION, for every row on the list — the same guard
    ``test_every_person_row_equals_its_own_collect_ceiling`` applies to the row,
    now applied to the screen. A future edit that keeps Alice right while
    breaking anyone else still fails here.
    """
    rows = [r for r in StaffService().paginate_cod_debtors_for_staff(page=1, per_page=100)["items"]
            if r.get("row_type") == "person"]
    assert rows, "the scenario must produce person rows or this test asserts nothing"

    for row in rows:
        statement = StaffService().get_customer_cod_statement_for_staff(row["id"])
        _addr, offer = CashCollectionHandler._collect_offer(statement)
        screen = CashCollectionHandler._format_statement(statement, "en")
        assert f"{offer:,.0f}" in _money_lines(screen)[0], (
            f"row {row['id']} is offered {offer} but its screen heads with "
            f"{_money_lines(screen)[0]!r}"
        )
        assert offer == row["total_outstanding_amount"]
