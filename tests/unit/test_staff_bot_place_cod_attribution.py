"""Plan E — no coworker selection; attribution follows the orderer.

Everything here drives the REAL handler against a REAL-shaped payload (the shape
emitted by CashCollectionService.get_customer_cod_statement, pinned in
test_staff_bot_place_surfaces.py's fixtures). Nothing pre-bakes the handler's own
intermediate state.

OWNER RULING A7 (2026-08-05) deleted the place-statement screen and the 🏢 row
that reached it, so every collection now starts from a PERSON row. The tests that
pinned that screen — and `_place_collection_anchor`, which existed only to name a
`customer_id` for a post nobody had selected a person for — went with it. What
survives is the person-row path, which is how the office's debt is collected now.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared import business_config
from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler


def _make_update_context(callback_data=None, message_text=None):
    update = MagicMock()
    update.effective_user = MagicMock(id=999)
    if message_text is None:
        update.callback_query = MagicMock()
        update.callback_query.data = callback_data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.message = None
    else:
        update.callback_query = None
        update.message = MagicMock()
        update.message.text = message_text
        update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {"language": "en", "authenticated": True,
                         "staff_roles": ["delivery_driver"]}
    context.bot = MagicMock()
    return update, context


class _AsyncClient:
    def __init__(self, **methods):
        self.client = MagicMock()
        for name, mock in methods.items():
            setattr(self.client, name, mock)

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _ok(data):
    return MagicMock(success=True, data=data)


def _patch_handler(monkeypatch, handler, module, client):
    monkeypatch.setattr(module, "api_client", client)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))


def _edited_markup(update):
    return update.callback_query.edit_message_text.call_args.kwargs.get("reply_markup")


def _edited_text(update):
    return update.callback_query.edit_message_text.call_args.args[0]


def _callbacks(update):
    return [b.callback_data
            for row in _edited_markup(update).inline_keyboard for b in row]


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", False)


@pytest.mark.unit
def test_the_person_row_is_the_drivers_only_debtor_selection(gate_on):
    """🔴 E13 + A7 — THE WHOLE CRUX IN ONE ASSERTION. Do not delete this test.

    Supersedes `test_place_screen_deletion_does_not_remove_person_row_selection`,
    whose second half drove the place screen A7 removed. The first half is the
    part that mattered, and it is now the ONLY selection there is.

    The owner ruled twice, emphatically: "NO second picker. The standalone COD
    debt by nature itself contains user/debtor selection." A7 then removed the
    other family outright: "the debtors list only shows the users, and the office
    debt is included in each coworker's debt."

    So the list emits `staff_cod_customer_<id>` and nothing else that opens a
    screen. If this fails because a `staff_cod_place_*` callback is back, someone
    has reinstated the place doorway — and `staff_bot/bot.py` has no handler for
    it. If it fails because `staff_cod_customer_*` is gone, someone has deleted
    the driver's real debtor selection. Both are regressions.
    """
    from staff_bot.keyboards.delivery import DeliveryKeyboards

    markup = DeliveryKeyboards.cod_debtor_list(
        "en",
        [
            # A row of the family the engine still emits; the bot must ignore it
            # rather than render a button nothing handles (deploy skew).
            {"row_type": "place", "place_group_id": 7, "label": "Acme office",
             "member_count": 2, "total_outstanding_amount": 35000.0},
            {"row_type": "person", "id": 11, "first_name": "Alice",
             "last_name": "Member", "phone": "+998901112233",
             "cluster_member_count": 1, "total_outstanding_amount": 45000.0},
        ],
        1,
        1,
    )
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "staff_cod_customer_11" in callbacks
    assert not any(c.startswith("staff_cod_place_") for c in callbacks)


def customer_statement(**overrides):
    """Alice (11): her own account owes 15 000, her cluster owes 15 000, and the
    place she belongs to owes 35 000 (hers + Bob's 20 000).

    ``place_collect_ceiling_amount`` is the field
    ``StaffService.get_customer_cod_statement_for_staff`` publishes (A6/R-B):
    her cluster's 15 000 UNION the place's debt that is not already hers, i.e.
    15 000 + Bob's 20 000 = 35 000. It is written out as a LITERAL here rather
    than derived from the other keys, so a fixture can never launder a broken
    composition into a passing assertion. The end-to-end pin that the server
    really produces this number, from real rows, is
    ``test_cod_collect_ceiling_row_pin.py``.
    """
    statement = {
        "customer_id": 11, "first_name": "Alice", "last_name": "Member",
        "phone": "+998901112233",
        "active_cod_debt_count": 1,
        "account_active_cod_debt_count": 1,
        "total_outstanding_amount": 15000.0,
        "cluster_member_count": 1,
        "cluster_delivered_outstanding_amount": 15000.0,
        "places": [{"address_id": 42, "place_group_id": 7, "label": "Acme office",
                    "place_open_cod_debt_total": 35000.0,
                    "place_active_cod_debt_count": 2,
                    "place_collect_ceiling_amount": 35000.0,
                    "place_collect_ceiling_debt_count": 2}],
        "items": [{"order_number": "ORD-1", "outstanding_amount": 15000.0,
                   "order_status": "delivered"}],
    }
    statement.update(overrides)
    return statement


def _run_start_collection(monkeypatch, which, statement, pre_user_data=None, customer_id=11):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    prefix = "staff_cod_collect_full_" if which == "full" else "staff_cod_collect_custom_"
    update, context = _make_update_context(callback_data=f"{prefix}{customer_id}")
    context.user_data.update(pre_user_data or {})
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(return_value=_ok(statement)))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    method = handler.start_full_collection if which == "full" else handler.start_custom_collection
    asyncio.run(method(update, context))
    return context.user_data.get("pending_cod_collection_flow")


def _run_receive_amount(monkeypatch, statement, text, customer_id=11,
                        delivery_address_id=42):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context(message_text=text)
    context.user_data["pending_cod_collection_flow"] = {
        "customer_id": customer_id, "customer_name": "Alice Member",
        "customer_phone": "+998901112233",
        # `start_custom_collection` is the ONLY producer of a flow that reaches
        # this step (staff_bot/bot.py:1092-1100 routes here only while `amount`
        # is unset). With the gate ON and a published ceiling it stores 42;
        # with the gate off — or with no published ceiling — it stores None,
        # because the scope and the ceiling are one decision (P0-degraded).
        # Priced from THIS field, never from a fresh resolution; see
        # test_overpayment_threshold_follows_the_flows_place_not_a_fresh_resolution.
        "delivery_address_id": delivery_address_id,
    }
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(return_value=_ok(statement)))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    state = asyncio.run(handler.receive_collection_amount(update, context))
    return state, context.user_data.get("pending_cod_collection_flow")


def _run_custom_then_amount(monkeypatch, statement, text, later_statement=None,
                            customer_id=11):
    """Drive the REAL two-step custom flow on ONE context.

    Step 1 "Collect custom" (``start_custom_collection``) resolves and stores
    the scope address; step 2 (``receive_collection_amount``) re-fetches the
    statement and prices the overpayment threshold. ``later_statement``, when
    given, is what step 2 sees — the world moving between the two steps (an
    admin re-grouping this customer's addresses, most plausibly), which is what
    makes "price the STORED address, never a fresh resolution" load-bearing.
    """
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    start_update, context = _make_update_context(
        callback_data=f"staff_cod_collect_custom_{customer_id}"
    )
    served = [statement, later_statement if later_statement is not None else statement]
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(
        side_effect=[_ok(s) for s in served]
    ))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.start_custom_collection(start_update, context))

    amount_update, _ = _make_update_context(message_text=text)
    state = asyncio.run(handler.receive_collection_amount(amount_update, context))
    return state, context.user_data.get("pending_cod_collection_flow")


@pytest.mark.unit
def test_resolved_place_returns_the_address_and_its_total(gate_on):
    """The SSOT pair (E4). The ceiling and the posted address can never name
    two different places."""
    assert CashCollectionHandler._resolved_place(customer_statement()) == (42, 35000.0)


@pytest.mark.unit
def test_resolved_place_reports_zero_total_when_the_gate_is_off(gate_off):
    """The ADDRESS still resolves — it is posted as the scope seed today — but
    no place TOTAL is published, so nothing can widen."""
    assert CashCollectionHandler._resolved_place(customer_statement()) == (42, 0.0)


@pytest.mark.unit
@pytest.mark.parametrize("second_place_group_id", [8, 7])
def test_resolved_place_is_zero_when_the_customer_has_two_places(gate_on, second_place_group_id):
    """E7, and after A7 it is the ONLY outcome for a two-place customer.

    Supersedes `test_resolved_place_follows_the_tapped_place` and
    `test_resolved_place_refuses_a_tapped_place_this_customer_is_not_in`
    (E20's Q5 guard), both of which turned on `pending_place_group_id` — a key
    only the deleted place screen ever wrote. With no tap there is nothing to
    follow and no cross-place to refuse; two places is simply ambiguous, and
    ambiguity must not be guessed.

    The parametrisation pins that NOTHING about the second place's identity can
    resurrect a preference — a duplicate group id resolves to `None` just the
    same.
    """
    statement = customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 35000.0},
        {"address_id": 43, "place_group_id": second_place_group_id,
         "place_open_cod_debt_total": 1000.0},
    ])
    assert CashCollectionHandler._resolved_place(statement) == (None, 0.0)


@pytest.mark.unit
def test_no_user_data_can_name_a_place_any_more(gate_on):
    """🔴 A7 — THE DELETED DOORWAY, PINNED SHUT. Do not delete this test.

    `_resolved_place` used to accept the driver's telegram context and prefer
    `context.user_data["pending_place_group_id"]`. That key was written by
    exactly one place — `show_place_statement` — and A7 deleted it. This test
    asserts the API itself no longer offers the seam: the method takes the
    statement and nothing else, so no future caller can smuggle a place choice
    back in without changing this signature deliberately.
    """
    import inspect

    params = list(inspect.signature(
        CashCollectionHandler._resolved_place.__func__
    ).parameters)
    assert params == ["cls", "statement"], params
    # Neither resolver may read the driver's telegram state at all. (The
    # docstrings still NAME the deleted key as history — `user_data` is the term
    # that would have to come back for a doorway to exist.)
    for func in (CashCollectionHandler._resolved_place.__func__,
                 CashCollectionHandler._resolve_scope_address_id):
        assert "user_data" not in inspect.getsource(func), func.__name__


@pytest.mark.unit
def test_full_collection_offers_the_whole_place_total(monkeypatch, gate_on):
    """R1/R3: the driver at the office door collects the OFFICE's debt, not the
    slice booked on the person standing in front of them."""
    flow = _run_start_collection(monkeypatch, "full", customer_statement())
    assert flow["amount"] == 35000.0
    assert flow["delivery_address_id"] == 42


@pytest.mark.unit
def test_full_collection_ceiling_is_the_union_not_the_max(monkeypatch, gate_on):
    """🔴 THE A6 P0. Do not weaken this back to a max.

    Alice's cluster owes 90 000 of its own and the office owes 35 000, of which
    20 000 is Bob's. The settlement is ring 1 ∪ ring 2 = 110 000 and her debtor
    row reads 110 000, but the shipped ceiling was
    ``max(90 000, 90 000, 35 000) = 90 000``: the list advertised a total the
    collect flow refused, and the 20 000 in between was settling a coworker's
    debt while the overpayment confirmation called it prepayment.
    """
    flow = _run_start_collection(
        monkeypatch, "full",
        customer_statement(
            cluster_member_count=2,
            cluster_delivered_outstanding_amount=90000.0,
            places=[{"address_id": 42, "place_group_id": 7, "label": "Acme office",
                     "place_open_cod_debt_total": 35000.0,
                     "place_active_cod_debt_count": 2,
                     # 90 000 of her own ∪ Bob's 20 000.
                     "place_collect_ceiling_amount": 110000.0,
                     "place_collect_ceiling_debt_count": 3}],
        ),
    )
    assert flow["amount"] == 110000.0


@pytest.mark.unit
def test_full_collection_ignores_an_unresolvable_place(monkeypatch, gate_on):
    """Two places and none tapped: offering either total would post the cash at
    the wrong workplace (E7)."""
    statement = customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 35000.0},
        {"address_id": 43, "place_group_id": 8, "place_open_cod_debt_total": 1000.0},
    ])
    flow = _run_start_collection(monkeypatch, "full", statement)
    assert flow["amount"] == 15000.0
    assert flow["delivery_address_id"] is None


@pytest.mark.unit
def test_full_collection_ceiling_unchanged_when_the_gate_is_off(monkeypatch, gate_off):
    flow = _run_start_collection(monkeypatch, "full", customer_statement())
    assert flow["amount"] == 15000.0


@pytest.mark.unit
def test_a_debt_free_member_can_still_start_a_full_collection(monkeypatch, gate_on):
    """A7/R-F end to end on the flow: the office's debt is collected THROUGH A
    PERSON, including a person who owes nothing of their own. Without the place
    arm, `start_full_collection` bails out with the 'no COD debt' alert and the
    driver cannot take a single sum from the coworker standing in front of them.

    (Renamed from `test_a_debt_free_anchor_can_still_start_a_place_collection`:
    there is no anchor and no place collection any more, but the capability the
    test was really guarding is exactly what A7 relies on.)"""
    statement = customer_statement(
        active_cod_debt_count=0, account_active_cod_debt_count=0,
        total_outstanding_amount=0.0, cluster_delivered_outstanding_amount=0.0,
        items=[],
    )
    flow = _run_start_collection(monkeypatch, "full", statement)
    assert flow["amount"] == 35000.0


@pytest.mark.unit
def test_debt_free_member_is_offered_collect_on_their_own_statement(monkeypatch, gate_on):
    """R1 safety net: the per-person can_collect gate no longer hides the action
    from someone whose workplace owes."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context(callback_data="staff_cod_customer_11")
    statement = customer_statement(
        active_cod_debt_count=0, account_active_cod_debt_count=0,
        total_outstanding_amount=0.0, cluster_delivered_outstanding_amount=0.0,
        items=[],
    )
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(return_value=_ok(statement)))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    asyncio.run(handler.show_customer_statement(update, context))

    assert any(c.startswith("staff_cod_collect_full_") for c in _callbacks(update))


@pytest.mark.unit
def test_debt_free_member_gets_only_back_when_the_gate_is_off(monkeypatch, gate_off):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context(callback_data="staff_cod_customer_11")
    statement = customer_statement(
        active_cod_debt_count=0, account_active_cod_debt_count=0,
        total_outstanding_amount=0.0, cluster_delivered_outstanding_amount=0.0,
        items=[],
    )
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(return_value=_ok(statement)))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    asyncio.run(handler.show_customer_statement(update, context))

    assert len(_edited_markup(update).inline_keyboard) == 1  # back only


@pytest.mark.unit
def test_amount_within_the_place_total_is_not_an_overpayment(monkeypatch, gate_on):
    """The false-warning defect: collecting the office's whole total from one
    member is not a surplus, and must not be announced as prepayment."""
    from staff_bot.handlers.delivery import cash_collection as mod

    state, flow = _run_receive_amount(
        monkeypatch,
        {**customer_statement(), "places": customer_statement()["places"]},
        "35000",
    )
    assert flow["amount"] == 35000.0
    assert "pending_overpayment_amount" not in flow
    assert state == mod.COLLECTION_NOTE_INPUT


@pytest.mark.unit
def test_amount_above_the_place_total_still_warns(monkeypatch, gate_on):
    """Above the PLACE total, rings 1 and 2 really are exhausted, so the
    existing 'becomes customer prepayment' copy is now TRUE (E5)."""
    from staff_bot.handlers.delivery import cash_collection as mod

    state, flow = _run_receive_amount(monkeypatch, customer_statement(), "50000")
    assert flow["pending_overpayment_amount"] == 50000.0
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM


@pytest.mark.unit
def test_overpayment_threshold_unchanged_when_the_gate_is_off(monkeypatch, gate_off):
    from staff_bot.handlers.delivery import cash_collection as mod

    state, flow = _run_receive_amount(monkeypatch, customer_statement(), "35000")
    assert flow["pending_overpayment_amount"] == 35000.0
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM
    # P0-degraded: this step re-decides the scope alongside the threshold, so a
    # stale place address on the flow (here seeded to 42 by the helper) is
    # dropped rather than posted under a cluster-only ceiling.
    assert flow["delivery_address_id"] is None


@pytest.mark.unit
def test_place_total_for_address_prices_the_address_it_is_given(gate_on):
    """E4's SSOT for a step that has ALREADY committed to a place.

    The ceiling is priced from the address the flow will post against, so the
    two can never name different places — no context, no re-resolution, nothing
    that can move between the two moments.
    """
    statement = customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 5000.0},
        {"address_id": 43, "place_group_id": 8, "place_open_cod_debt_total": 90000.0},
    ])
    assert CashCollectionHandler._place_total_for_address(statement, 42) == 5000.0
    assert CashCollectionHandler._place_total_for_address(statement, 43) == 90000.0
    # E7's ambiguity refusal arrives here as "no address was resolved".
    assert CashCollectionHandler._place_total_for_address(statement, None) == 0.0
    # An address that names none of this customer's places widens nothing.
    assert CashCollectionHandler._place_total_for_address(statement, 99) == 0.0


@pytest.mark.unit
def test_place_total_for_address_is_zero_when_the_gate_is_off(gate_off):
    """C0: the amount step's widening is gated exactly like every other one."""
    assert CashCollectionHandler._place_total_for_address(customer_statement(), 42) == 0.0


@pytest.mark.unit
def test_overpayment_threshold_follows_the_flows_place_not_a_fresh_resolution(
    monkeypatch, gate_on
):
    """🔴 E4 / invariant 1 on the CUSTOM path. Do not delete this test.

    Unlike `start_full_collection` — one resolution, one instant — the custom
    path spans two handler invocations: "Collect custom" stores the scope
    address, and the typed amount prices the threshold against a FRESHLY
    FETCHED statement. Those two moments are not protected by a
    ConversationHandler state (`receive_collection_amount` is dispatched by the
    catch-all text router, staff_bot/bot.py:1092-1100), so the world can move
    between them.

    A7 removed the sharpest version of that (a tap on a still-live place button
    in an older message, which `show_place_statement` used to record), but the
    seam itself is intact: here an admin groups a SECOND address for this
    customer between the two steps. A fresh resolution would now find two places,
    refuse to guess (E7), drop to the cluster's 15 000 and fire the overpayment
    prompt over money that is still settling a coworker's debt. Pricing the
    STORED address keeps the offer and the post one place.
    """
    from staff_bot.handlers.delivery import cash_collection as mod

    one_place = customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 5000.0,
         "place_collect_ceiling_amount": 20000.0},
    ])
    regrouped = customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 5000.0,
         "place_collect_ceiling_amount": 20000.0},
        {"address_id": 43, "place_group_id": 8, "place_open_cod_debt_total": 90000.0,
         "place_collect_ceiling_amount": 105000.0},
    ])
    state, flow = _run_custom_then_amount(
        monkeypatch, one_place, "50000", later_statement=regrouped,
    )

    assert flow["delivery_address_id"] == 42          # the committed place
    # Place 42's own published ceiling, NOT the cluster fallback a fresh
    # (now ambiguous) resolution would have produced.
    assert flow["total_outstanding_amount"] == 20000.0
    assert flow["pending_overpayment_amount"] == 50000.0
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM


@pytest.mark.unit
def test_the_custom_path_still_widens_to_its_own_places_total(monkeypatch, gate_on):
    """The mirror of the test above: pinning the threshold to the flow's place
    must not disable widening. Nothing moves, so the office's whole 35 000 is
    collectible from one member without a false prepayment warning."""
    from staff_bot.handlers.delivery import cash_collection as mod

    state, flow = _run_custom_then_amount(monkeypatch, customer_statement(), "35000")

    assert flow["delivery_address_id"] == 42
    assert flow["total_outstanding_amount"] == 35000.0
    assert flow["amount"] == 35000.0
    assert "pending_overpayment_amount" not in flow
    assert state == mod.COLLECTION_NOTE_INPUT


@pytest.mark.unit
def test_the_custom_path_threshold_is_unchanged_when_the_gate_is_off(monkeypatch, gate_off):
    """C0 on the same two-step path: gate OFF ⇒ no place widening at all, so
    35 000 against a 15 000 personal debt still warns, exactly as in Plan D.

    🔴 AND THE SCOPE DEGRADES WITH IT (P0-degraded). The gate-off ceiling is the
    cluster's own 15 000, so posting `delivery_address_id` would give the engine
    PLACE scope and settle ring 1 ∪ ring 2 under a cluster-only threshold — the
    surplus copy would promise 20 000 of prepayment that pays Bob instead. Plan D
    posted no `delivery_address_id` at all (the key does not exist in this file
    at HEAD), so `None` here is what "unchanged" actually means.
    """
    from staff_bot.handlers.delivery import cash_collection as mod

    state, flow = _run_custom_then_amount(monkeypatch, customer_statement(), "35000")

    assert flow["delivery_address_id"] is None
    assert flow["total_outstanding_amount"] == 15000.0
    assert flow["pending_overpayment_amount"] == 35000.0
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM
