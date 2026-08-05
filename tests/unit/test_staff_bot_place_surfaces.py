"""Staff-bot PLACE surfaces (plan 2c Task 12).

A "place" is a grouped delivery address — one physical workplace reached from
several phone numbers. Phase 2c makes the driver- and operator-facing screens
place-aware:

* the order card / at-door cash prompt state the place's whole open COD total;
* the COD debtor list mixes PLACE rows with cluster-collapsed PERSON rows;
* a new place statement lists every member's open debt (names only, no phones);
* a standalone collection carries ``delivery_address_id`` so the 2b scope engine
  can resolve PLACE scope for an order-less collection;
* the bottle statement / qty picker / fine prompt anchor on the PLACE balance
  (``place_balance``), and the fine itself is posted keyed by ``address_id``;
* the operator's COD-block copy names the arm that actually fired.

Two defects fixed here are pinned explicitly:

1. ``DeliveryKeyboards.cod_debtor_list`` used to do ``c['id']`` on every row.
   Place rows have no ``id`` — the list crashed with ``KeyError: 'id'`` the
   moment any place group existed
   (``test_debtor_keyboard_place_row_has_no_id_key_and_must_not_raise``).
2. The operator relayed "customer has unpaid orders" even when a COWORKER's
   debt at a shared workplace caused the block
   (``test_operator_person_and_place_notices_differ``).

Translation keys are seeded by Task 15, so every assertion on rendered copy
either stubs ``i18n.get`` or relies on staff_bot's deterministic missing-key
fallback (``staff_bot/i18n.py:114-118`` — humanised last key segment).
"""

import asyncio
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.api_client import StaffAPIClient
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler
from staff_bot.handlers.operator.create_order import CreateOrderHandler
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.utils.formatters import format_order_card

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOT_FILE = REPO_ROOT / "staff_bot" / "bot.py"


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


def _make_update_context(callback_data=None, message_text=None):
    """Update/context pair satisfying @require_auth + @require_delivery_driver."""
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
    context.user_data = {
        "language": "en",
        "authenticated": True,
        "staff_roles": ["delivery_driver", "operator"],
    }
    context.bot = MagicMock()
    return update, context


class _AsyncClient:
    """Async-context-manager stand-in for the module-level ``api_client``."""

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


def _edited_text(update):
    call = update.callback_query.edit_message_text.call_args
    return call.args[0] if call.args else call.kwargs["text"]


def _edited_markup(update):
    call = update.callback_query.edit_message_text.call_args
    return call.kwargs.get("reply_markup")


def _patch_handler(monkeypatch, handler, module, client):
    monkeypatch.setattr(module, "api_client", client)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))


@pytest.fixture
def captured_i18n(monkeypatch):
    """Echo the key and record kwargs, for exact key/interpolation assertions.

    Mandatory for copy assertions: staff_bot's ``i18n.get`` humanises the last
    key segment when a key is missing, which would make these tests depend on
    the Task 15 translation seed.
    """
    calls = []

    def fake_get(key, language=None, *args, **kwargs):
        calls.append({"key": key, "language": language, "kwargs": kwargs})
        return key

    return calls, fake_get


# ---------------------------------------------------------------------------
# 1. Order card — place COD total
# ---------------------------------------------------------------------------


def _cash_order(**overrides):
    order = {
        "order_number": "ORD-1",
        "customer_name": "Alice",
        "customer_phone": "+99890",
        "district": "Chilonzor",
        "address": "Office st 1",
        "total_amount": 15000,
        "payment_method": "cash",
        "outstanding_amount": 15000,
        "expected_cash_to_collect": 15000,
        "cod_reserved_prepayment_amount": 0,
        "item_count": 1,
    }
    order.update(overrides)
    return order


@pytest.mark.unit
def test_order_card_shows_place_cod_total_when_grouped():
    card = format_order_card(
        _cash_order(
            is_place_grouped=True,
            place_group_label="Acme office",
            place_outstanding_cod_total=35000.0,
            place_active_cod_debt_count=2,
        ),
        "en",
    )

    assert "Place cod total" in card  # humanised staff.delivery.place_cod_total
    assert "35,000" in card
    assert "Acme office" in card


@pytest.mark.unit
def test_order_card_unchanged_when_ungrouped():
    """Ungrouped baseline: the card must be byte-identical to today's."""
    ungrouped = format_order_card(
        _cash_order(
            is_place_grouped=False,
            place_outstanding_cod_total=0.0,
            place_active_cod_debt_count=0,
        ),
        "en",
    )
    legacy = format_order_card(_cash_order(), "en")

    assert "Place cod total" not in ungrouped
    assert ungrouped == legacy


@pytest.mark.unit
def test_order_card_omits_place_line_when_place_has_no_open_debt():
    """A grouped address with nothing outstanding adds no noise to the card."""
    card = format_order_card(
        _cash_order(
            is_place_grouped=True,
            place_group_label="Acme office",
            place_outstanding_cod_total=0.0,
            place_active_cod_debt_count=0,
        ),
        "en",
    )
    assert "Place cod total" not in card


# ---------------------------------------------------------------------------
# 1b. At-door cash prompt + the snapshot that feeds it
# ---------------------------------------------------------------------------


_DELIVERED_SNAPSHOT = {
    "delivery_id": 5, "order_number": "ORD-1", "status": "arrived",
    "customer_name": "Alice", "customer_phone": "+99890",
    "district": "Chilonzor", "address": "Office st 1",
    "items": [{"product_name": "19L", "quantity": 3}],
    "payment_method": "cash", "payment_status": "pending", "total_amount": 15000,
    "amount_collected": 0, "outstanding_amount": 15000,
    "expected_cash_to_collect": 15000, "cod_reserved_prepayment_amount": 0,
    "expected_returnable_bottles": 0, "customer_bottle_balance": 0,
}


def _run_initiate_delivered(monkeypatch, snapshot):
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    handler = StatusUpdateHandler()
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    update, context = _make_update_context(callback_data="staff_status_5_delivered")
    context.user_data["current_delivery"] = dict(snapshot)
    asyncio.run(handler.initiate_status_change(update, context))
    return _edited_text(update)


@pytest.mark.unit
def test_at_door_cash_prompt_shows_place_cod_total(monkeypatch):
    """At a grouped workplace the driver must see the WHOLE place's open COD."""
    text = _run_initiate_delivered(monkeypatch, dict(
        _DELIVERED_SNAPSHOT,
        is_place_grouped=True, place_group_label="Acme office",
        place_outstanding_cod_total=35000.0, place_active_cod_debt_count=2,
    ))
    assert "Place cod total" in text
    assert "35,000" in text


@pytest.mark.unit
def test_at_door_cash_prompt_unchanged_when_ungrouped(monkeypatch):
    text = _run_initiate_delivered(monkeypatch, _DELIVERED_SNAPSHOT)
    assert "Place cod total" not in text


@pytest.mark.unit
def test_active_delivery_snapshot_carries_place_keys(monkeypatch):
    """``current_delivery`` WHITELISTS keys, and the at-door prompt reads only
    that snapshot — so the Task 3 place fields must be copied into it."""
    from staff_bot.handlers.delivery import active_delivery as mod
    from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler

    handler = ActiveDeliveryHandler()
    update, context = _make_update_context(callback_data="staff_view_active_5")
    delivery = dict(
        _DELIVERED_SNAPSHOT,
        is_place_grouped=True, place_group_id=7, place_group_label="Acme office",
        place_outstanding_cod_total=35000.0, place_active_cod_debt_count=2,
    )
    client = _AsyncClient(get_active_deliveries=AsyncMock(return_value=_ok({"items": [delivery]})))
    _patch_handler(monkeypatch, handler, mod, client)

    asyncio.run(handler.view_active_delivery(update, context))

    snapshot = context.user_data["current_delivery"]
    assert snapshot["is_place_grouped"] is True
    assert snapshot["place_group_label"] == "Acme office"
    assert snapshot["place_outstanding_cod_total"] == 35000.0
    assert snapshot["place_active_cod_debt_count"] == 2


# ---------------------------------------------------------------------------
# 2. COD debtor list keyboard — USER ROWS ONLY (owner ruling A7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_debtor_keyboard_renders_person_rows_only():
    """🔴 A7 — THE KEYBOARD HALF OF THE REMOVAL. Do not delete this test.

    Supersedes `test_debtor_keyboard_renders_place_and_cluster_rows` and
    `test_debtor_keyboard_unlabelled_place_falls_back_to_group_id`, both of which
    pinned the 🏢 button A7 removed ("in staff bot there won't be any 'office'
    row in debtors list").

    The cluster annotation on the surviving person row is asserted here too, so
    deleting the place assertions costs no coverage of what stays.
    """
    rows = [
        {"row_type": "place", "place_group_id": 7, "label": "Acme office",
         "member_count": 2, "active_cod_debt_count": 2, "total_outstanding_amount": 35000.0},
        {"row_type": "person", "id": 11, "first_name": "Solo", "last_name": "Debtor",
         "phone": "+99890", "active_cod_debt_count": 1, "cluster_member_count": 2,
         "member_user_ids": [11, 12], "total_outstanding_amount": 15000.0},
    ]
    markup = DeliveryKeyboards.cod_debtor_list("en", rows, 1, 1)
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]

    assert not any(c.startswith("staff_cod_place_") for c in callbacks)
    assert "staff_cod_customer_11" in callbacks
    assert not any("Acme office" in btn.text
                   for row in markup.inline_keyboard for btn in row)

    person_text = next(
        btn.text
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data == "staff_cod_customer_11"
    )
    assert "👥" in person_text  # linked-cluster annotation


@pytest.mark.unit
def test_debtor_keyboard_skips_a_place_row_instead_of_raising():
    """LIVE crash regression, kept alive across the A7 deletion.

    Place rows carry ``place_group_id`` but no ``id``. The old keyboard did
    ``c['id']`` for every row, so the driver's whole COD debtor list raised
    ``KeyError: 'id'`` as soon as one place group existed. A7 stops the service
    emitting that family, but a staff_bot newer than its business_app can still
    be handed one during a deploy — so the row must be SKIPPED, never rendered
    and never subscripted.

    (Supersedes `test_debtor_keyboard_place_row_has_no_id_key_and_must_not_raise`,
    which asserted the row rendered a `staff_cod_place_7` button.)
    """
    place_row = {
        "row_type": "place", "place_group_id": 7, "label": "Acme office",
        "member_count": 2, "active_cod_debt_count": 2, "total_outstanding_amount": 35000.0,
    }
    assert "id" not in place_row  # the exact shape the engine emits

    markup = DeliveryKeyboards.cod_debtor_list("en", [place_row], 1, 1)

    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert not any(c.startswith("staff_cod_place_") for c in callbacks)
    assert not any(c.startswith("staff_cod_customer_") for c in callbacks)
    # Only the Back row survives — an empty debtor screen, not a crash.
    assert callbacks == ["staff_cash_hub"]


@pytest.mark.unit
def test_debtor_keyboard_singleton_person_row_unchanged():
    """Unlinked + ungrouped baseline: no cluster annotation, same label as today."""
    legacy_row = {"id": 11, "first_name": "Aziz", "last_name": "Debtor",
                  "phone": "+998900000999", "active_cod_debt_count": 1,
                  "total_outstanding_amount": 50000.0}
    markup = DeliveryKeyboards.cod_debtor_list("en", [legacy_row], 1, 1)
    text = markup.inline_keyboard[0][0].text

    assert text.startswith("👤 Aziz Debtor")
    assert "👥" not in text
    assert "50,000" in text

    # A row that explicitly says "one member" must render identically.
    singleton = dict(legacy_row, row_type="person", cluster_member_count=1,
                     member_user_ids=[11])
    same = DeliveryKeyboards.cod_debtor_list("en", [singleton], 1, 1)
    assert same.inline_keyboard[0][0].text == text


# ---------------------------------------------------------------------------
# 3. Place statement handler — DELETED by owner ruling A7
# ---------------------------------------------------------------------------
#
# `show_place_statement`, the `PLACE_STATEMENT` fixture and the four tests that
# drove it (`..._lists_orders_with_owner_names`,
# `..._offers_collect_actions_not_member_rows`,
# `..._records_tapped_group_for_scope`, `..._leaks_no_member_phone_numbers`) are
# all gone: A7 removed the screen, so there is no longer a surface for them to
# describe. The capability they protected — collecting the office's whole debt in
# one action — now lives on the PERSON row and is pinned end to end, against real
# rows, in tests/unit/test_cod_collect_ceiling_row_pin.py.


# ---------------------------------------------------------------------------
# 4. Customer statement — cluster / place / per-account lines
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_statement_headline_is_the_collect_offer_call_itself(monkeypatch):
    """🔴 THE FIFTH-INSTANCE STRUCTURAL PIN. Do not delete.

    The defect was not a wrong number — it was two independent expressions of
    "how much", one for the human and one for the engine, with nothing forcing
    them to describe the same set. So this asserts the SEAM, not a value: stub
    ``_collect_offer`` with a figure that exists nowhere in the payload and
    require it on the screen. A formatter that recomputed its own headline (from
    ``total_outstanding_amount``, from ``places[]``, from anything) renders one of
    the payload's numbers instead and fails here, no matter how plausible its
    arithmetic is.
    """
    payload = {
        "first_name": "Aziz", "last_name": "Debtor", "phone": "+99890",
        "active_cod_debt_count": 2,
        "total_outstanding_amount": 25000,
        "cluster_member_count": 1,
        "cluster_delivered_outstanding_amount": 25000,
        "places": [{"address_id": 42, "place_group_id": 7, "label": "Acme office",
                    "place_open_cod_debt_total": 35000,
                    "place_collect_ceiling_amount": 45000}],
        "items": [{"order_number": "ORD-1", "outstanding_amount": 15000}],
    }
    monkeypatch.setattr(
        CashCollectionHandler, "_collect_offer",
        classmethod(lambda _cls, _statement: (42, 777777.0)),
    )

    text = CashCollectionHandler._format_statement(payload, "en")

    assert "777,777" in text, text
    # None of the payload's own money figures may head the screen.
    money_lines = [line for line in text.splitlines() if "Uzs" in line]
    assert "777,777" in money_lines[0], money_lines


@pytest.mark.unit
def test_statement_shows_cluster_total_and_places_when_linked():
    text = CashCollectionHandler._format_statement(
        {
            "first_name": "Aziz", "last_name": "Debtor", "phone": "+99890",
            "active_cod_debt_count": 2,
            "account_active_cod_debt_count": 1,
            "total_outstanding_amount": 15000,
            "cluster_member_count": 2,
            "cluster_delivered_outstanding_amount": 35000,
            "places": [{"address_id": 42, "place_group_id": 7, "label": "Acme office",
                        "place_open_cod_debt_total": 35000, "place_active_cod_debt_count": 2}],
            "items": [{"order_number": "ORD-1", "outstanding_amount": 15000,
                       "order_status": "delivered"}],
        },
        "en",
    )

    assert "Cluster debt total" in text
    assert "35,000" in text
    assert "Acme office" in text


@pytest.mark.unit
def test_statement_states_per_account_count_when_it_differs_from_cluster():
    """``active_cod_debt_count`` is CLUSTER-wide while ``items`` are PER-ACCOUNT,
    so a linked sibling with no debts of their own would read '2 active debts'
    over an empty list. The payload carries the per-account count — state it."""
    text = CashCollectionHandler._format_statement(
        {
            "first_name": "Sibling", "last_name": "Account", "phone": "+99891",
            "active_cod_debt_count": 2,
            "account_active_cod_debt_count": 0,
            "total_outstanding_amount": 0,
            "cluster_member_count": 2,
            "cluster_delivered_outstanding_amount": 35000,
            "places": [],
            "items": [],
        },
        "en",
    )

    assert "Account cod debts" in text
    # The per-account zero must be visible next to the cluster's 2.
    account_line = next(line for line in text.splitlines() if "Account cod debts" in line)
    assert account_line.rstrip().endswith("0")


@pytest.mark.unit
def test_statement_unchanged_for_unlinked_ungrouped_customer():
    """Singleton baseline: no cluster line, no place line, no account line."""
    payload = {
        "first_name": "Aziz", "last_name": "Debtor", "phone": "+998900000999",
        "active_cod_debt_count": 1,
        "account_active_cod_debt_count": 1,
        "total_outstanding_amount": 90000,
        "cluster_member_count": 1,
        "cluster_delivered_outstanding_amount": 90000,
        "places": [],
        "items": [{"order_number": "AD_000281_26", "outstanding_amount": 90000}],
    }
    text = CashCollectionHandler._format_statement(payload, "en")

    assert "Cluster debt total" not in text
    assert "Account cod debts" not in text
    # For a singleton with no place, the offer IS the cluster figure, so the
    # headline reads the same 90 000 it always did.
    assert "90,000" in text


@pytest.mark.unit
def test_a_legacy_payload_states_what_that_payload_can_actually_collect():
    """Supersedes the byte-equality half of the singleton baseline above.

    That assertion compared a full payload's screen to one stripped of
    ``cluster_delivered_outstanding_amount`` and demanded they render
    IDENTICALLY — which was only true because the headline came from the raw
    ``total_outstanding_amount``, i.e. from the very field the fifth-instance fix
    removed. It was a pin on the defect, not on a behaviour worth keeping.

    A payload with no cluster figure is a `business_app` older than this bot. The
    flow offers 0 there (``_scoped_ceiling``'s base is that missing field) and
    ``start_full_collection`` refuses with "no outstanding COD debt". So the
    screen must say 0 — advertising 90 000 over a flow that refuses to take a
    cent is exactly the invariant `staff_service.py:2414` forbids.
    """
    legacy = {
        "first_name": "Aziz", "last_name": "Debtor", "phone": "+998900000999",
        "active_cod_debt_count": 1,
        "total_outstanding_amount": 90000,
        "items": [{"order_number": "AD_000281_26", "outstanding_amount": 90000}],
    }

    assert CashCollectionHandler._collect_offer(legacy) == (None, 0.0)

    text = CashCollectionHandler._format_statement(legacy, "en")
    headline = next(line for line in text.splitlines() if "Collectible now" in line)
    assert "0 Uzs" in headline, headline
    assert "90,000" not in headline, headline


# ---------------------------------------------------------------------------
# 5. Standalone collection — cluster total + PLACE scope address
# ---------------------------------------------------------------------------


def _customer_statement(**overrides):
    """A statement as ``GET /staff/customers/<id>/cod-statement`` actually serves
    it with the gate ON — i.e. every place carrying its published
    ``place_collect_ceiling_amount``.

    🔴 THAT FIELD IS LOAD-BEARING, NOT DECORATION. `_scoped_ceiling` returns a
    postable ``delivery_address_id`` ONLY together with that address's own
    published ceiling; drop the key and the flow correctly degrades to
    cluster scope with ``delivery_address_id=None``. A fixture without it does
    not describe a live backend, it describes the deploy-skew window — which is
    pinned on real rows in ``test_cod_collect_ceiling_row_pin.py``.
    """
    statement = {
        "customer_id": 11, "first_name": "Alice", "last_name": "Member",
        "phone": "+99890", "active_cod_debt_count": 2,
        "account_active_cod_debt_count": 1,
        "total_outstanding_amount": 15000.0,
        "cluster_member_count": 2,
        "cluster_delivered_outstanding_amount": 35000.0,
        "places": [{"address_id": 42, "place_group_id": 7, "label": "Acme office",
                    "place_open_cod_debt_total": 35000.0, "place_active_cod_debt_count": 2,
                    # cluster 35 000 ∪ the coworkers' share of the place — the
                    # same union the debtor row publishes (A6/R-B).
                    "place_collect_ceiling_amount": 35000.0,
                    "place_collect_ceiling_debt_count": 2}],
        "items": [{"order_number": "ORD-1", "outstanding_amount": 15000.0,
                   "order_status": "delivered"}],
    }
    statement.update(overrides)
    return statement


def _run_start_collection(monkeypatch, which, statement, pre_user_data=None):
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    prefix = "staff_cod_collect_full_" if which == "full" else "staff_cod_collect_custom_"
    update, context = _make_update_context(callback_data=f"{prefix}11")
    context.user_data.update(pre_user_data or {})
    client = _AsyncClient(get_customer_cod_statement=AsyncMock(return_value=_ok(statement)))
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    method = handler.start_full_collection if which == "full" else handler.start_custom_collection
    asyncio.run(method(update, context))
    return context.user_data.get("pending_cod_collection_flow")


@pytest.mark.unit
def test_full_collection_uses_cluster_total_when_larger(monkeypatch):
    """The driver settles the linked person's whole delivered debt, not just the
    slice booked on the account they tapped."""
    flow = _run_start_collection(monkeypatch, "full", _customer_statement())
    assert flow["amount"] == 35000.0


@pytest.mark.unit
def test_full_collection_keeps_per_account_total_when_cluster_is_not_larger(monkeypatch):
    """Singleton baseline: cluster total == account total ⇒ today's amount."""
    flow = _run_start_collection(
        monkeypatch, "full",
        _customer_statement(cluster_member_count=1,
                            cluster_delivered_outstanding_amount=15000.0, places=[]),
    )
    assert flow["amount"] == 15000.0
    assert flow["delivery_address_id"] is None


@pytest.mark.unit
def test_full_collection_scope_address_is_none_when_place_is_ambiguous(monkeypatch):
    """Two places: posting an arbitrary address would spread the cash over the
    wrong workplace, so scope stays cluster/personal.

    Supersedes `test_full_collection_scope_address_prefers_the_tapped_place` —
    A7 deleted the place screen, so nothing can name a place for the driver and
    "ambiguous" is now the only outcome for a two-place customer."""
    statement = _customer_statement(places=[
        {"address_id": 42, "place_group_id": 7, "place_open_cod_debt_total": 35000.0},
        {"address_id": 43, "place_group_id": 8, "place_open_cod_debt_total": 1000.0},
    ])
    flow = _run_start_collection(monkeypatch, "full", statement)
    assert flow["delivery_address_id"] is None


@pytest.mark.unit
def test_custom_collection_stores_scope_address(monkeypatch):
    flow = _run_start_collection(monkeypatch, "custom", _customer_statement())
    assert flow["delivery_address_id"] == 42
    assert flow["customer_id"] == 11


@pytest.mark.unit
@pytest.mark.parametrize("which", ["full", "custom"])
def test_no_published_ceiling_means_no_place_scoped_post(monkeypatch, which):
    """🔴 P0-degraded, at the handler seam. Do not relax to a ceiling assertion.

    A `staff_bot` newer than its `business_app` receives the raw engine statement
    with no `place_collect_ceiling_amount`. The offer then degrades to the
    cluster-only figure — and the SCOPE must degrade with it, or the post stays
    PLACE-scoped, settles ring 1 ∪ ring 2 well above the ceiling, and the surplus
    the driver is promised never exists. Measured against the real engine in
    `test_cod_collect_ceiling_row_pin.py`.
    """
    places = [{"address_id": 42, "place_group_id": 7, "label": "Acme office",
               "place_open_cod_debt_total": 35000.0, "place_active_cod_debt_count": 2}]
    flow = _run_start_collection(monkeypatch, which, _customer_statement(places=places))
    assert flow["delivery_address_id"] is None
    if which == "full":
        assert flow["amount"] == 35000.0          # the cluster's own debt only


@pytest.mark.unit
def test_custom_amount_does_not_warn_overpayment_within_cluster_debt(monkeypatch):
    """Collecting the linked person's whole delivered debt is not an overpayment:
    the per-account ``total_outstanding_amount`` must not trigger the surplus
    confirmation when the cluster owes at least that much."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context(message_text="35000")
    context.user_data["pending_cod_collection_flow"] = {
        "customer_id": 11, "customer_name": "Alice", "customer_phone": "+99890",
    }
    client = _AsyncClient(
        get_customer_cod_statement=AsyncMock(return_value=_ok(_customer_statement()))
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    state = asyncio.run(handler.receive_collection_amount(update, context))

    flow = context.user_data["pending_cod_collection_flow"]
    assert flow["amount"] == 35000.0
    assert "pending_overpayment_amount" not in flow
    assert state == mod.COLLECTION_NOTE_INPUT


@pytest.mark.unit
def test_custom_amount_still_warns_on_a_real_overpayment(monkeypatch):
    """Above the cluster's debt the surplus confirmation still fires."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    handler = CashCollectionHandler()
    update, context = _make_update_context(message_text="50000")
    context.user_data["pending_cod_collection_flow"] = {
        "customer_id": 11, "customer_name": "Alice", "customer_phone": "+99890",
    }
    client = _AsyncClient(
        get_customer_cod_statement=AsyncMock(return_value=_ok(_customer_statement()))
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    state = asyncio.run(handler.receive_collection_amount(update, context))

    flow = context.user_data["pending_cod_collection_flow"]
    assert flow["pending_overpayment_amount"] == 50000.0
    assert state == mod.COLLECTION_OVERPAYMENT_CONFIRM


@pytest.mark.unit
def test_standalone_collection_payload_carries_delivery_address_id(monkeypatch):
    """Spec 8: an order-less standalone collection at a grouped address can only
    reach PLACE scope through ``delivery_address_id``."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    posted = {}

    async def _record(token, payload):
        posted.update(payload)
        return _ok({"cash_collection_event": {"id": 1}})

    handler = CashCollectionHandler()
    update, context = _make_update_context(message_text="collected at the office")
    context.user_data["pending_cod_collection_flow"] = {
        "customer_id": 11, "amount": 30000.0, "total_outstanding_amount": 30000.0,
        "customer_name": "Alice", "customer_phone": "+99890",
        "delivery_address_id": 42,
    }
    client = _AsyncClient(
        record_cash_collection=AsyncMock(side_effect=_record),
        get_customer_cod_statement=AsyncMock(return_value=_ok({"total_outstanding_amount": 0})),
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    asyncio.run(handler.receive_collection_note(update, context))

    assert posted["delivery_address_id"] == 42
    assert posted["customer_id"] == 11
    assert posted["amount"] == 30000.0


@pytest.mark.unit
def test_standalone_collection_payload_without_place_posts_none(monkeypatch):
    """Ungrouped baseline: the key is present but null, i.e. today's scope."""
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    posted = {}

    async def _record(token, payload):
        posted.update(payload)
        return _ok({"cash_collection_event": {"id": 1}})

    handler = CashCollectionHandler()
    update, context = _make_update_context(message_text="note")
    context.user_data["pending_cod_collection_flow"] = {
        "customer_id": 11, "amount": 1000.0, "customer_name": "Solo", "customer_phone": "+99890",
    }
    client = _AsyncClient(
        record_cash_collection=AsyncMock(side_effect=_record),
        get_customer_cod_statement=AsyncMock(return_value=_ok({"total_outstanding_amount": 0})),
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())

    asyncio.run(handler.receive_collection_note(update, context))

    assert posted["delivery_address_id"] is None


# `test_debtor_list_clears_stale_tapped_place` was deleted with owner ruling A7:
# it pinned `show_debtor_list` popping `pending_place_group_id`, a key that only
# the (now removed) place screen ever wrote. `test_no_user_data_can_name_a_place_any_more`
# in test_staff_bot_place_cod_attribution.py pins the stronger property that no
# user_data key can influence place resolution at all.


# ---------------------------------------------------------------------------
# 6. Bottle surfaces — place balances, statement actions, fine payload
#
# ANTI-BLIND-SPOT NOTE. Every fixture below — no exceptions — is built by one
# of the four factories that follow, and those factories are pinned against the
# REAL backend payloads by the contract tests in 6e, in KEYS *and* in VALUE.
# The previous version of this section fabricated `balance` / `total_balance` /
# `group_union_balance` / `bottle_balance_id` as literal dicts and therefore
# stayed GREEN through the whole (user, address) → PLACE re-key while drivers
# could not issue a single fine. Add new fixtures through the factories, or
# the blind spot comes straight back — a literal dict here is invisible to 6e.
# ---------------------------------------------------------------------------


def _summary_address(**overrides):
    """One ``get_customer_summary()['addresses']`` row.

    Keyed ``place_balance`` — one row per address the customer OWNS, so two
    owned addresses in one group describe the SAME place twice.
    """
    row = {
        "address_id": 44,
        "address_title": "work",
        "full_address": "1 Office St, Tashkent",
        "place_balance": 7.0,
        "last_delivery_at": None,
        "last_return_at": None,
        "address_group_id": 9,
        "is_grouped": True,
    }
    row.update(overrides)
    return row


def _cluster_scope(**overrides):
    """One ``get_customer_summary()['cluster_scopes']`` row.

    These rows are keyed ``balance``, NOT ``place_balance`` — the one place
    that spelling survives — and there is exactly one per DISTINCT place, which
    is why they are the only sound basis for a driver-facing total.
    """
    row = {"address_group_id": 9, "address_id": None, "balance": 7.0, "is_shared": True}
    row.update(overrides)
    return row


def _bottle_summary(addresses=None, cluster_scopes=None, **overrides):
    """A whole ``get_customer_summary()`` payload. Note there is no scalar
    total by design."""
    summary = {
        "user_id": 11,
        "addresses": [_summary_address()] if addresses is None else addresses,
        "active_fines_count": 0,
        "total_fine_amount": 0.0,
        "is_linked": True,
        "cluster_member_ids": [11, 12],
        "cluster_scopes": [_cluster_scope()] if cluster_scopes is None else cluster_scopes,
    }
    summary.update(overrides)
    return summary


def _place_row(**overrides):
    """One ``get_customer_place_rows()`` row — ONE row per PLACE, carrying an
    address the customer owns (lowest id wins) and no ``bottle_balance_id``."""
    row = {
        "address_id": 44,
        "address_title": "work",
        "full_address": "1 Office St, Tashkent",
        "is_grouped": True,
        "place_group_id": 9,
        "place_balance": 7.0,
    }
    row.update(overrides)
    return row


def _build_fine_payload(flow, quantity, fine_amount, notes):
    """Late-bound so a missing helper fails the tests that need it rather than
    erroring the whole module at import."""
    return getattr(BottleCollectionHandler, "_build_fine_payload")(
        flow, quantity, fine_amount, notes
    )


def _actionable_places(summary):
    return getattr(BottleCollectionHandler, "_actionable_places")(summary)


# --- 6a. Driver-issued fines (100% broken: BottleFine has no bottle_balance_id)


@pytest.mark.unit
def test_fine_posts_address_id_not_bottle_balance_id():
    """Driver fines are 100% broken today: BottleFine has no bottle_balance_id."""
    body = _build_fine_payload(flow={"customer_id": 7, "address_id": 44},
                               quantity=2, fine_amount=50000, notes=None)
    assert body["address_id"] == 44
    assert "bottle_balance_id" not in body


@pytest.mark.unit
def test_fine_payload_carries_the_whole_route_contract():
    """``business_app/api/staff.py`` rejects the POST unless customer_id,
    address_id, quantity and fine_amount are all present and truthy."""
    body = _build_fine_payload(flow={"customer_id": 7, "address_id": 44},
                               quantity=2, fine_amount=50000, notes="two missing")
    assert body == {
        "customer_id": 7,
        "address_id": 44,
        "quantity": 2,
        "fine_amount": 50000,
        "notes": "two missing",
    }


def _run_receive_fine_note(monkeypatch, pre_flow, note="two missing"):
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    handler = BottleCollectionHandler()
    update, context = _make_update_context(message_text=note)
    context.user_data["pending_bottle_collection_flow"] = dict(pre_flow)
    client = _AsyncClient(
        create_bottle_fine=AsyncMock(return_value=_ok({"id": 1})),
        get_customer_bottle_summary=AsyncMock(return_value=_ok(_bottle_summary())),
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.receive_fine_note(update, context))
    return update, context, client


@pytest.mark.unit
def test_receive_fine_note_posts_the_place_keyed_body(monkeypatch):
    """END-TO-END on broken flow #1. Today the handler re-fetches /summary,
    scans it for a ``bottle_balance_id`` that no payload has carried since
    migration a3e7d1f9c204 dropped the column, finds nothing and bails to a
    generic error — so ``create_bottle_fine`` is never even called."""
    _, _, client = _run_receive_fine_note(
        monkeypatch, {"customer_id": 11, "address_id": 44, "action": "fine",
                      "fine_quantity": 2, "fine_amount": 50000.0}
    )

    assert client.client.create_bottle_fine.await_count == 1
    posted = client.client.create_bottle_fine.await_args.args[1]
    assert posted["customer_id"] == 11
    assert posted["address_id"] == 44
    assert posted["quantity"] == 2
    assert posted["fine_amount"] == 50000.0
    assert posted["notes"] == "two missing"
    assert "bottle_balance_id" not in posted


@pytest.mark.unit
def test_receive_fine_note_does_not_refetch_the_summary(monkeypatch):
    """``start_fine`` already put the address in the flow; the round trip that
    existed only to find a dropped column is pure latency at the door."""
    _, _, client = _run_receive_fine_note(
        monkeypatch, {"customer_id": 11, "address_id": 44, "action": "fine",
                      "fine_quantity": 2, "fine_amount": 50000.0}
    )
    client.client.get_customer_bottle_summary.assert_not_awaited()


@pytest.mark.unit
def test_receive_fine_note_bails_when_the_flow_lost_the_address(monkeypatch):
    """The null guard survives the deletion of the lookup block: no address, no
    fine — the route requires one."""
    update, _, client = _run_receive_fine_note(
        monkeypatch, {"customer_id": 11, "action": "fine",
                      "fine_quantity": 2, "fine_amount": 50000.0}
    )
    client.client.create_bottle_fine.assert_not_awaited()
    assert update.message.reply_text.call_args.args[0] == "Error occurred"


# --- 6b. Statement screen actions (100% broken: the action list is always [])


@pytest.mark.unit
def test_statement_offers_actions_for_a_place_with_empties():
    summary = _bottle_summary(addresses=[_summary_address()])
    rows = _actionable_places(summary)
    assert len(rows) == 1               # today: 0, so the screen dead-ends


@pytest.mark.unit
def test_two_owned_addresses_in_one_place_collapse_to_one_row():
    summary = _bottle_summary(addresses=[
        _summary_address(),
        _summary_address(address_id=45, address_title="work2"),
    ])
    assert len(_actionable_places(summary)) == 1


@pytest.mark.unit
def test_actionable_places_keeps_an_over_returned_place():
    """Filtered on ``!= 0``, not ``> 0``: there is nothing to collect at an
    over-returned place, but a fine is still issuable there."""
    summary = _bottle_summary(addresses=[_summary_address(place_balance=-3.0)])
    assert [r["address_id"] for r in _actionable_places(summary)] == [44]


@pytest.mark.unit
def test_actionable_places_drops_a_zero_place():
    summary = _bottle_summary(addresses=[_summary_address(place_balance=0.0)])
    assert _actionable_places(summary) == []


@pytest.mark.unit
def test_actionable_places_keeps_a_grouped_and_an_ungrouped_place_apart():
    summary = _bottle_summary(addresses=[
        _summary_address(),
        _summary_address(address_id=45, address_title="home", place_balance=2.0,
                         is_grouped=False, address_group_id=None),
    ])
    assert [r["address_id"] for r in _actionable_places(summary)] == [44, 45]


@pytest.mark.unit
def test_actionable_places_never_collapses_two_ungrouped_addresses():
    """The dedup key must fall back to the ADDRESS for an ungrouped row. Keying
    everything on ``address_group_id`` would map both of these to ``('g', None)``
    and silently hide one of the customer's two solo places."""
    summary = _bottle_summary(addresses=[
        _summary_address(address_id=44, address_title="home", place_balance=5.0,
                         is_grouped=False, address_group_id=None),
        _summary_address(address_id=45, address_title="dacha", place_balance=2.0,
                         is_grouped=False, address_group_id=None),
    ])
    rows = _actionable_places(summary)

    assert [r["address_id"] for r in rows] == [44, 45]
    assert [r["place_balance"] for r in rows] == [5.0, 2.0]


def _run_show_bottle_statement(monkeypatch, summary=None, place_rows=None,
                               addresses_response=None, handler=None):
    from staff_bot.handlers.delivery import bottle_collection as mod

    handler = handler or BottleCollectionHandler()
    update, context = _make_update_context(callback_data="staff_bottle_customer_11")
    client = _AsyncClient(
        get_customer_bottle_summary=AsyncMock(
            return_value=_ok(_bottle_summary() if summary is None else summary)
        ),
        get_customer_bottle_addresses=AsyncMock(
            return_value=addresses_response if addresses_response is not None
            else _ok([_place_row()] if place_rows is None else place_rows)
        ),
    )
    _patch_handler(monkeypatch, handler, mod, client)
    asyncio.run(handler.show_customer_bottle_statement(update, context))
    return update, context, client


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


@pytest.mark.unit
def test_statement_screen_renders_collect_and_fine_buttons(monkeypatch):
    """END-TO-END on broken flow #2. The picker used to filter ``balance > 0``
    on a key no payload carries, so the list was always empty and the driver
    got a bare Back button — Collect and Fine were unreachable."""
    update, _, _ = _run_show_bottle_statement(monkeypatch)

    callbacks = _callbacks(_edited_markup(update))
    assert "staff_bottle_collect_11_44" in callbacks
    assert "staff_bottle_fine_11_44" in callbacks


@pytest.mark.unit
def test_statement_screen_keeps_fine_but_hides_collect_when_over_returned(monkeypatch):
    """``can_collect`` has existed on the keyboard since day one and has never
    been passed. An over-returned place has nothing to collect."""
    update, _, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[_summary_address(place_balance=-3.0)],
                                cluster_scopes=[_cluster_scope(balance=-3.0)]),
        place_rows=[_place_row(place_balance=-3.0)],
    )

    callbacks = _callbacks(_edited_markup(update))
    assert "staff_bottle_fine_11_44" in callbacks
    assert "staff_bottle_collect_11_44" not in callbacks


@pytest.mark.unit
def test_statement_screen_reports_a_failed_addresses_call(monkeypatch):
    """A timeout / 500 / expired token on the picker's endpoint must reach the
    error handler. Swallowing it into an empty list would print the balance
    above a bare Back button — a non-zero balance the driver cannot act on,
    with nothing saying why. That is the exact screen this task removes."""
    handler = BottleCollectionHandler()
    reported = AsyncMock()
    monkeypatch.setattr(handler, "_handle_api_response_error", reported)

    failed = MagicMock(success=False, data=None, error="upstream timeout",
                       status_code=504, error_code=None)
    update, _, _ = _run_show_bottle_statement(
        monkeypatch, addresses_response=failed, handler=handler
    )

    assert reported.await_count == 1
    assert reported.await_args.args[1] is failed
    # ...and the dead-end screen was never rendered.
    update.callback_query.edit_message_text.assert_not_called()


@pytest.mark.unit
def test_statement_screen_still_shows_the_empty_state_on_a_successful_empty_call(
    monkeypatch,
):
    """The counterpart: "the call failed" and "the call succeeded with nothing
    actionable" are different screens and must not be collapsed."""
    handler = BottleCollectionHandler()
    reported = AsyncMock()
    monkeypatch.setattr(handler, "_handle_api_response_error", reported)

    update, _, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[_summary_address(place_balance=0.0)],
                                cluster_scopes=[_cluster_scope(balance=0.0)]),
        place_rows=[],
        handler=handler,
    )

    reported.assert_not_awaited()
    callbacks = _callbacks(_edited_markup(update))
    assert not any(c.startswith("staff_bottle_collect_") for c in callbacks)
    assert not any(c.startswith("staff_bottle_fine_") for c in callbacks)
    assert "No bottle balance" in _edited_text(update)


@pytest.mark.unit
def test_statement_picker_is_sourced_from_the_addresses_endpoint(monkeypatch):
    """D7. ``/summary`` offers one row per OWNED address while ``/addresses``
    (which the qty cap reads) returns one per PLACE, keyed to the lowest-id
    owned address. Offering address 45 from the summary dead-ends the driver,
    because the cap lookup can only ever match 44."""
    update, _, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[
            _summary_address(),
            _summary_address(address_id=45, address_title="work2"),
        ]),
        place_rows=[_place_row()],
    )

    callbacks = _callbacks(_edited_markup(update))
    assert "staff_bottle_collect_11_44" in callbacks
    assert not any(c.endswith("_45") for c in callbacks)


@pytest.mark.unit
def test_statement_screen_offers_one_button_per_place_when_several(monkeypatch):
    update, _, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[
            _summary_address(),
            _summary_address(address_id=45, address_title="home", place_balance=2.0,
                             is_grouped=False, address_group_id=None),
        ]),
        place_rows=[
            _place_row(),
            _place_row(address_id=45, address_title="home", place_balance=2.0,
                       is_grouped=False, place_group_id=None),
        ],
    )

    callbacks = _callbacks(_edited_markup(update))
    assert "staff_bottle_addr_11_44" in callbacks
    assert "staff_bottle_addr_11_45" in callbacks


# --- 6c. Every balance renders 0 (statement body, total, picker cap, labels)


@pytest.mark.unit
def test_statement_body_renders_the_place_balance(monkeypatch):
    update, _, _ = _run_show_bottle_statement(monkeypatch)
    text = _edited_text(update)

    assert "work" in text
    assert "7" in text
    assert "👥" in text                      # shared place marker
    assert "No bottle balance" not in text


@pytest.mark.unit
def test_statement_total_is_summed_from_cluster_scopes():
    """``get_customer_summary`` returns NO scalar total by design; summing
    ``place_balance`` over ``addresses`` double-counts a place the customer
    owns two addresses at. ``cluster_scopes`` is one row per DISTINCT place."""
    text = BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(
            addresses=[
                _summary_address(),
                _summary_address(address_id=45, address_title="work2"),
            ],
            cluster_scopes=[_cluster_scope(), _cluster_scope(address_group_id=10, balance=2.0)],
        ),
        "en",
    )

    total_line = next(line for line in text.splitlines() if "Total bottles" in line)
    assert total_line.endswith(": 9")


@pytest.mark.unit
def test_statement_body_lists_a_shared_place_once():
    text = BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(addresses=[
            _summary_address(),
            _summary_address(address_id=45, address_title="work2"),
        ]),
        "en",
    )
    assert len([line for line in text.splitlines() if line.startswith("•")]) == 1


@pytest.mark.unit
def test_statement_body_says_so_when_every_place_is_empty():
    """Pre-existing hole: the old empty-state fired only when the customer
    owned ZERO addresses, so a customer with addresses and all-zero places got
    a header and nothing else."""
    text = BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(addresses=[_summary_address(place_balance=0.0)],
                        cluster_scopes=[_cluster_scope(balance=0.0)]),
        "en",
    )
    assert "No bottle balance" in text


def _run_start_bottle_collection(monkeypatch, addr_rows):
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    handler = BottleCollectionHandler()
    update, context = _make_update_context(callback_data="staff_bottle_collect_11_44")
    client = _AsyncClient(
        get_customer_bottle_addresses=AsyncMock(return_value=_ok(addr_rows))
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.start_collection(update, context))
    return update, context


@pytest.mark.unit
@pytest.mark.parametrize("row,expected", [
    (_place_row(place_balance=9.0), 9),
    (_place_row(place_balance=3.0, is_grouped=False, place_group_id=None), 3),
])
def test_bottle_qty_picker_caps_at_the_place_balance(monkeypatch, row, expected):
    """One place, one pool — grouped or not. The 'own balance vs union'
    distinction is gone, and ``get_customer_place_rows`` emits neither of the
    keys this used to read, so the cap was always 0 and the flow dead-ended."""
    _, context = _run_start_bottle_collection(monkeypatch, [row])
    assert context.user_data["pending_bottle_collection_flow"]["balance"] == expected


@pytest.mark.unit
def test_address_selection_keyboard_labels_the_place_balance():
    """``bottle_address_selection`` renders whatever the caller filtered, and
    it read ``balance`` — so every button was labelled ``(0)``."""
    markup = DeliveryKeyboards.bottle_address_selection("en", 11, [_place_row()])
    label = markup.inline_keyboard[0][0].text

    assert "(7)" in label
    assert "👥" in label
    assert "work" in label


@pytest.mark.unit
def test_address_selection_keyboard_marks_an_over_returned_place():
    markup = DeliveryKeyboards.bottle_address_selection(
        "en", 11, [_place_row(place_balance=-3.0)]
    )
    label = markup.inline_keyboard[0][0].text

    assert "(↩3)" in label
    assert "(-3)" not in label


@pytest.mark.unit
def test_address_selection_keyboard_leaves_a_solo_place_unmarked():
    markup = DeliveryKeyboards.bottle_address_selection(
        "en", 11,
        [_place_row(address_title="home", place_balance=2.0, is_grouped=False,
                    place_group_id=None)],
    )
    label = markup.inline_keyboard[0][0].text

    assert "(2)" in label
    assert "👥" not in label


# --- 6d. Fine hint — the producer, not just the pre-baked flow


def _run_start_fine(monkeypatch, pre_flow, callback_data="staff_bottle_fine_11_44"):
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    handler = BottleCollectionHandler()
    update, context = _make_update_context(callback_data=callback_data)
    context.user_data["pending_bottle_collection_flow"] = dict(pre_flow)
    _patch_handler(monkeypatch, handler, mod, client=_AsyncClient())
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.start_fine(update, context))
    return _edited_text(update)


@pytest.mark.unit
def test_fine_prompt_shows_place_hint_when_grouped(monkeypatch):
    text = _run_start_fine(monkeypatch, {"customer_id": 11, "place_balances": {44: 7.0}})
    assert "Fine place union hint" in text


@pytest.mark.unit
def test_fine_prompt_has_no_hint_when_ungrouped(monkeypatch):
    text = _run_start_fine(monkeypatch, {"customer_id": 11, "place_balances": {}})
    assert "Fine place union hint" not in text


@pytest.mark.unit
def test_fine_hint_is_produced_from_a_real_shaped_summary(monkeypatch):
    """The old version of this test pre-baked ``place_unions`` INTO the flow, so
    it never touched the producer and stayed green while the producer returned
    ``{}`` on every payload in existence. Drive the statement screen first."""
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    update, context, _ = _run_show_bottle_statement(monkeypatch)

    handler = BottleCollectionHandler()
    fine_update, _ = _make_update_context(callback_data="staff_bottle_fine_11_44")
    _patch_handler(monkeypatch, handler, mod, client=_AsyncClient())
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    asyncio.run(handler.start_fine(fine_update, context))

    assert "Fine place union hint" in _edited_text(fine_update)


@pytest.mark.unit
def test_bottle_statement_records_place_balances_per_address(monkeypatch):
    """The balance per grouped address is captured while the summary is on
    screen, so the fine prompt needs no extra round trip. Ungrouped addresses
    stay out of the map."""
    _, context, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[
            _summary_address(),
            _summary_address(address_id=45, address_title="home", place_balance=3.0,
                             is_grouped=False, address_group_id=None),
        ]),
    )

    flow = context.user_data["pending_bottle_collection_flow"]
    assert flow["place_balances"] == {44: 7.0}
    # Nothing ever read this back — it must not be resurrected.
    assert "place_union_balance" not in flow
    assert "place_unions" not in flow


@pytest.mark.unit
def test_place_balances_keeps_an_over_returned_place(monkeypatch):
    """No ``> 0`` filter: Task 4 needs the negative value to say so."""
    _, context, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(addresses=[_summary_address(place_balance=-3.0)],
                                cluster_scopes=[_cluster_scope(balance=-3.0)]),
        place_rows=[_place_row(place_balance=-3.0)],
    )
    assert context.user_data["pending_bottle_collection_flow"]["place_balances"] == {44: -3.0}


# --- 6e. Contract pins — the fabrications above must match the real payloads


def _seed_place_empties(db, place, sample_user, second_sample_user):
    """Seed the shared place so its POOL is 7 while NEITHER member's own slice
    is: 5 delivered to the viewer's door, 2 to the coworker's.

    That asymmetry is deliberate. It is what makes the value assertions below
    decisive — a backend regression that keeps the ``place_balance`` key but
    reverts it to a per-user slice would report 5, not 7, and key-presence
    assertions alone would sail straight past it. 7 is also exactly the value
    the fabricated fixtures above carry, so the fixtures are pinned to the
    POOLED semantics, not merely to a number that happens to match.
    """
    from decimal import Decimal

    from business_app.services.bottle_tracking_service import BottleTrackingService
    from shared.enums import BottleLedgerEventType

    service = BottleTrackingService()
    service._create_ledger_entry(
        user_id=sample_user.id,
        address_id=place["a1"].id,
        event_type=BottleLedgerEventType.DELIVERY,
        quantity=Decimal("5"),
    )
    service._create_ledger_entry(
        user_id=second_sample_user.id,
        address_id=place["a2"].id,
        event_type=BottleLedgerEventType.DELIVERY,
        quantity=Decimal("2"),
    )
    db.session.flush()


@pytest.mark.integration
def test_fabricated_summary_matches_the_real_get_customer_summary_payload(
    app, db, place, sample_user, second_sample_user
):
    """THE anti-blind-spot guard. Everything above feeds literal dicts to the
    real formatters, so nothing above notices a backend rename. Derive the real
    payload once and assert the fabrication matches it in KEYS and in VALUE."""
    from business_app.services.bottle_tracking_service import BottleTrackingService

    _seed_place_empties(db, place, sample_user, second_sample_user)
    real = BottleTrackingService().get_customer_summary(sample_user.id)

    assert set(_bottle_summary()) <= set(real)
    assert set(_summary_address()) <= set(real["addresses"][0])
    assert set(_cluster_scope()) <= set(real["cluster_scopes"][0])

    # VALUE pin: `place_balance` is the whole POOL at that door (5 + 2), not the
    # viewer's own 5. This is the substance of the entire re-key, and it is what
    # a key-set-only assertion cannot see.
    row = real["addresses"][0]
    assert row["place_balance"] == 7.0
    assert row["is_grouped"] is True
    assert row["address_group_id"] == place["group"].id
    # ...and the coworker reads the SAME pool from their own address.
    coworker = BottleTrackingService().get_customer_summary(second_sample_user.id)
    assert coworker["addresses"][0]["place_balance"] == 7.0
    # One row per DISTINCT place on cluster_scopes, carrying the same pool.
    assert [s["balance"] for s in real["cluster_scopes"]] == [7.0]
    assert real["cluster_scopes"][0]["is_shared"] is True

    # The keys this module used to fabricate are gone — pin their absence, or
    # a re-introduced alias would let the stale readers pass again.
    assert "total_balance" not in real
    assert "cluster_total_balance" not in real
    for stale in ("balance", "bottle_balance_id", "group_union_balance",
                  "place_union_balance"):
        assert stale not in row
    # ...except on cluster_scopes, where `balance` is the real key.
    assert "balance" in real["cluster_scopes"][0]
    assert "place_balance" not in real["cluster_scopes"][0]


@pytest.mark.integration
def test_fabricated_place_row_matches_the_real_addresses_payload(
    app, db, place, sample_user, second_sample_user
):
    from business_app.services.bottle_tracking_service import BottleTrackingService

    _seed_place_empties(db, place, sample_user, second_sample_user)
    rows = BottleTrackingService.get_customer_place_rows(sample_user.id)

    assert set(_place_row()) <= set(rows[0])

    # VALUE pin, same reasoning as above: the pool (5 + 2), not the viewer's 5.
    assert rows[0]["place_balance"] == 7.0
    assert rows[0]["is_grouped"] is True
    assert rows[0]["place_group_id"] == place["group"].id
    assert rows[0]["address_id"] == place["a1"].id

    for stale in ("balance", "bottle_balance_id", "place_union_balance",
                  "group_union_balance"):
        assert stale not in rows[0]


@pytest.mark.integration
def test_the_real_summary_and_addresses_payloads_agree_on_the_address_id(
    app, db, place, sample_user, second_sample_user
):
    """D7's live bug, at the source. A customer owning two addresses in one
    group gets two ``/summary`` rows but a single ``/addresses`` row, so a
    picker built from ``/summary`` offers an address the qty cap can never
    match."""
    from business_app.models.user import UserAddress
    from business_app.services.bottle_tracking_service import BottleTrackingService

    second = UserAddress(user_id=sample_user.id, title="work2",
                         address_group_id=place["group"].id,
                         full_address="1 Office St, Tashkent", city="Tashkent",
                         latitude=41.2747, longitude=69.2063)
    db.session.add(second)
    db.session.flush()
    _seed_place_empties(db, place, sample_user, second_sample_user)

    summary = BottleTrackingService().get_customer_summary(sample_user.id)
    rows = BottleTrackingService.get_customer_place_rows(sample_user.id)

    assert len(summary["addresses"]) == 2
    assert len(rows) == 1
    assert {r["address_id"] for r in rows} <= {a["address_id"] for a in summary["addresses"]}
    # Both summary rows describe the SAME pool — which is why summing them
    # would double-count, and why the picker must dedup.
    assert [a["place_balance"] for a in summary["addresses"]] == [7.0, 7.0]
    assert rows[0]["place_balance"] == 7.0
    # The re-sourced picker must therefore agree with the cap lookup.
    offered = {r["address_id"] for r in _actionable_places({"addresses": rows})}
    assert offered == {r["address_id"] for r in rows}
    # ...and deduping the summary shape yields the same single place.
    assert len(_actionable_places(summary)) == 1


# ---------------------------------------------------------------------------
# 7. api_client + bot wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_bot_has_no_place_doorway_left():
    """🔴 A7 — THE WIRING PIN. Do not delete this test.

    Supersedes `test_bot_registers_place_statement_callback` and
    `test_api_client_place_statement_hits_the_task3_endpoint`, which asserted the
    existence of the callback registration and the client call A7 removed.

    Three things must all be absent together, or the bot ships a button that
    answers nothing (or a handler nothing can reach):
      * no `staff_cod_place_*` CallbackQueryHandler in staff_bot/bot.py;
      * no `show_place_statement` on the handler class;
      * no `get_place_cod_statement` on the API client.

    The BACKEND route (`GET /staff/place-groups/<id>/cod-statement`) is
    deliberately kept — an older staff_bot pod still calls it during a rolling
    deploy — so this test says nothing about it.
    """
    text = BOT_FILE.read_text(encoding="utf-8")
    assert "staff_cod_place_" not in text
    assert "show_place_statement" not in text
    assert not hasattr(CashCollectionHandler, "show_place_statement")
    assert not hasattr(CashCollectionHandler, "_place_collection_anchor")
    assert not hasattr(StaffAPIClient, "get_place_cod_statement")


# ---------------------------------------------------------------------------
# 8. Operator COD-block copy — name the arm that fired
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_operator_place_scope_blames_the_workplace(captured_i18n, monkeypatch):
    calls, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)

    notice = CreateOrderHandler._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "place",
            "active_cod_debt_count": 0,  # clean personal record
            "place_active_cod_debt_count": 2,
        },
        "en",
    )

    assert notice == "staff.operator.cod_restricted_place"
    assert calls[0]["kwargs"] == {"place_active_cod_debt_count": 2}


@pytest.mark.unit
def test_operator_person_scope_keeps_todays_message(captured_i18n, monkeypatch):
    calls, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)

    notice = CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "person", "active_cod_debt_count": 2},
        "en",
    )

    assert notice == "staff.operator.cod_restricted"
    assert calls[0]["kwargs"] == {}


@pytest.mark.unit
def test_operator_missing_scope_falls_back_like_today(captured_i18n, monkeypatch):
    _, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)

    assert CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "active_cod_debt_count": 1}, "en"
    ) == "staff.operator.cod_restricted"
    assert CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": None}, "en"
    ) == "staff.operator.cod_restricted"


@pytest.mark.unit
def test_operator_person_and_place_notices_differ(captured_i18n, monkeypatch):
    """The defect: both arms used to render the same 'customer has unpaid
    orders' copy, so a coworker's debt was relayed to the wrong person."""
    _, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)

    place = CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place",
         "place_active_cod_debt_count": 2}, "en"
    )
    person = CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "person",
         "active_cod_debt_count": 2}, "en"
    )
    assert place != person
    assert place == "staff.operator.cod_restricted_place"
    assert person == "staff.operator.cod_restricted"


@pytest.mark.unit
def test_operator_place_notice_leaks_no_coworker_identity(captured_i18n, monkeypatch):
    """A count is fine; coworker names/phones are not (spec 7)."""
    _, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)
    calls, _ = captured_i18n

    CreateOrderHandler._cod_restriction_notice(
        {
            "cod_restricted": True,
            "restriction_scope": "place",
            "place_active_cod_debt_count": 2,
            # Hostile payload: even if identity ever appears alongside, the copy
            # must interpolate the count and nothing else.
            "place_debtor_names": ["Bob Coworker"],
            "place_debtor_phones": ["+998901112233"],
        },
        "en",
    )

    assert calls[0]["kwargs"] == {"place_active_cod_debt_count": 2}


@pytest.mark.unit
def test_operator_place_notice_degrades_when_count_missing(captured_i18n, monkeypatch):
    calls, fake_get = captured_i18n
    monkeypatch.setattr("staff_bot.handlers.operator.create_order.i18n.get", fake_get)

    notice = CreateOrderHandler._cod_restriction_notice(
        {"cod_restricted": True, "restriction_scope": "place"}, "en"
    )
    assert notice == "staff.operator.cod_restricted_place"
    assert calls[0]["kwargs"] == {"place_active_cod_debt_count": 0}


@pytest.mark.unit
def test_operator_payment_methods_request_carries_the_delivery_address():
    """Without the destination address the backend never evaluates the PLACE arm
    (business_app/api/staff.py:892-895), so the operator could never be told a
    workplace caused the block."""
    client = StaffAPIClient()
    seen = {}

    async def fake_make_request(method, endpoint, **kwargs):
        seen.update({"method": method, "endpoint": endpoint, "kwargs": kwargs})
        return _ok({})

    client._make_request = fake_make_request
    asyncio.run(client.get_operator_payment_methods("tok", 11, delivery_address_id=42))

    assert seen["kwargs"]["params"] == {"delivery_address_id": 42}

    seen.clear()
    asyncio.run(client.get_operator_payment_methods("tok", 11))
    assert not seen["kwargs"].get("params")
