"""Over-returned places as a FIRST-CLASS staff-bot state (plan D, task 4).

A place goes negative when more empties came back through that door than were
ever delivered there. Task 3 widened the actionable filter from ``> 0`` to
``!= 0``, so a negative place is now reachable on every driver screen — but
nothing yet *says* what it is. Each surface below must distinguish three
states, not two:

* **negative** — over-returned. A real record. Nothing to collect, but a fine
  is still issuable, so the screen must stay ACTIONABLE and never dead-end.
* **zero** — no empties on record.
* **positive** — the ordinary case.

Every fixture here comes from the four factories in
``tests/unit/test_staff_bot_place_surfaces.py``, which are pinned key-and-value
against the real backend payloads by that module's section 6e. Importing them
rather than re-fabricating them is deliberate: a literal dict in this file
would be invisible to those contract pins, which is exactly the blind spot that
kept the whole staff-bot bottle surface green while drivers could not issue a
single fine.

Copy assertions go through the ``i18n_spy`` fixture, which echoes the key back.
Without it ``staff_bot/i18n.py`` humanises a missing key's last segment and then
silently drops every interpolation kwarg, so an assertion on rendered text would
depend on the seed having run.
"""

import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from staff_bot.i18n import i18n
from tests.unit.test_staff_bot_place_surfaces import (
    _AsyncClient,
    _bottle_summary,
    _callbacks,
    _cluster_scope,
    _edited_markup,
    _edited_text,
    _make_update_context,
    _ok,
    _patch_handler,
    _place_row,
    _run_show_bottle_statement,
    _run_start_bottle_collection,
    _summary_address,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = "seed_staff_over_returned_translations"

OVER_RETURNED_KEYS = {
    "staff.delivery.place_over_returned",
    "staff.delivery.fine_place_over_returned_hint",
    "staff.delivery.bottles_return_prompt_over_returned",
    "staff.delivery.bottle_collection_recorded_over_returned",
}


@pytest.fixture
def i18n_spy(monkeypatch):
    """Echo each key and record its kwargs.

    ``i18n`` is a module-level singleton shared by every handler and keyboard,
    so patching ``.get`` on it covers all of them at once.
    """
    calls = []

    def fake_get(key, language=None, *args, **kwargs):
        calls.append({"key": key, "kwargs": kwargs})
        return key

    monkeypatch.setattr(i18n, "get", fake_get)
    return calls


def _kwargs_for(calls, key):
    return next(c["kwargs"] for c in calls if c["key"] == key)


# ---------------------------------------------------------------------------
# 1. Statement body — an over-returned place must be NAMED, not printed as "-3"
# ---------------------------------------------------------------------------


def _statement(balance):
    return BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(
            addresses=[_summary_address(place_balance=balance)],
            cluster_scopes=[_cluster_scope(balance=balance)],
        ),
        "en",
    )


@pytest.mark.unit
def test_statement_body_distinguishes_over_returned_zero_and_positive(i18n_spy):
    """Today the body prints a bare ``-3``, which reads like a typo at the door."""
    negative = _statement(-3.0)
    assert "staff.delivery.place_over_returned" in negative
    # ...on the body line AND on the signed cross-place total one row above it.
    # A bare "-3" anywhere on this screen reads as a bug at the customer's door.
    assert "-3" not in negative
    assert all(c["kwargs"] == {"count": "3"}
               for c in i18n_spy if c["key"] == "staff.delivery.place_over_returned")

    i18n_spy.clear()
    positive = _statement(7.0)
    assert "7" in positive
    assert "staff.delivery.place_over_returned" not in positive

    i18n_spy.clear()
    zero = _statement(0.0)
    assert "staff.delivery.no_bottle_balance" in zero
    assert "staff.delivery.place_over_returned" not in zero


@pytest.mark.unit
def test_statement_total_does_not_leak_binary_float_noise(i18n_spy):
    """The header total is a SUM, so float accumulation shows through: 1.1 + 2.2
    is 3.3000000000000003, and -1.1 + -2.2 would be announced as
    "over-returned by 3.3000000000000003"."""
    positive = BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(
            addresses=[_summary_address(place_balance=1.1),
                       _summary_address(address_id=45, address_title="dacha",
                                        place_balance=2.2, is_grouped=False,
                                        address_group_id=None)],
            cluster_scopes=[_cluster_scope(balance=1.1),
                            _cluster_scope(address_group_id=None, address_id=45,
                                           balance=2.2, is_shared=False)],
        ),
        "en",
    )
    total_line = next(l for l in positive.splitlines() if "total_bottles" in l)
    assert total_line.endswith(": 3.3")

    i18n_spy.clear()
    BottleCollectionHandler._format_bottle_statement(
        _bottle_summary(
            addresses=[_summary_address(place_balance=-1.1)],
            cluster_scopes=[_cluster_scope(balance=-1.1),
                            _cluster_scope(address_group_id=None, address_id=45,
                                           balance=-2.2, is_shared=False)],
        ),
        "en",
    )
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "3.3"}


@pytest.mark.unit
def test_the_signed_place_balance_is_spelled_identically_along_the_whole_chain():
    """H10 is a chain of three files that must agree on ONE string. The wire pin
    in tests/unit/test_plan_d_backend_additions.py catches a rename of the
    emitter; this catches renaming the emitter AND its pin while leaving either
    bot end behind, which would take the prompt silently offline again.

    Matches the QUOTED literal only. A bare substring check passes on the
    helper's *identifier* (`_place_bottle_balance_signed`), which is not the
    wire name and can be renamed independently — verified: renaming only the
    emitted key left a substring check green.
    """
    quoted = re.compile(r"""(['"])place_bottle_balance_signed\1""")
    for rel in (
        "business_app/api/staff.py",                        # emits
        "staff_bot/handlers/delivery/active_delivery.py",   # whitelists
        "staff_bot/handlers/delivery/status_update.py",     # branches on
    ):
        assert quoted.search((REPO_ROOT / rel).read_text(encoding="utf-8")), rel


@pytest.mark.unit
def test_statement_body_does_not_round_a_fractional_place_to_zero(i18n_spy):
    """``int()`` truncates toward zero, so a place at -0.5 survives the ``!= 0``
    filter and would otherwise be announced as "over-returned by 0"."""
    _statement(-0.5)
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "0.5"}


# ---------------------------------------------------------------------------
# 2. Multi-place picker → select_address must pass can_collect (the D4 gap)
# ---------------------------------------------------------------------------


def _run_select_address(monkeypatch, context, callback_data):
    """Re-enter the picker's callback on an EXISTING statement context."""
    from staff_bot.handlers.delivery import bottle_collection as mod

    handler = BottleCollectionHandler()
    update, _ = _make_update_context(callback_data=callback_data)
    _patch_handler(monkeypatch, handler, mod, _AsyncClient())
    asyncio.run(handler.select_address(update, context))
    return update


@pytest.mark.unit
def test_multi_place_picker_hides_collect_only_for_the_over_returned_place(monkeypatch):
    """Task 3's ``!= 0`` filter made a negative place reachable through the
    MULTI-place picker, which routes through ``select_address`` — and that path
    still defaults to ``can_collect=True``, so tapping Collect dead-ends on the
    ``balance <= 0`` guard downstream. The single-place shortcut already gets
    this right; this is the same defect one screen over.
    """
    statement_update, context, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(
            addresses=[
                _summary_address(),
                _summary_address(address_id=45, address_title="dacha", place_balance=-3.0,
                                 is_grouped=False, address_group_id=None),
            ],
            cluster_scopes=[_cluster_scope(), _cluster_scope(address_group_id=None,
                                                             address_id=45, balance=-3.0,
                                                             is_shared=False)],
        ),
        place_rows=[
            _place_row(),
            _place_row(address_id=45, address_title="dacha", place_balance=-3.0,
                       is_grouped=False, place_group_id=None),
        ],
    )
    offered = _callbacks(_edited_markup(statement_update))
    assert "staff_bottle_addr_11_44" in offered
    assert "staff_bottle_addr_11_45" in offered

    negative = _callbacks(_edited_markup(
        _run_select_address(monkeypatch, context, "staff_bottle_addr_11_45")
    ))
    assert "staff_bottle_fine_11_45" in negative          # still actionable
    assert "staff_bottle_collect_11_45" not in negative   # nothing to collect

    positive = _callbacks(_edited_markup(
        _run_select_address(monkeypatch, context, "staff_bottle_addr_11_44")
    ))
    assert "staff_bottle_collect_11_44" in positive
    assert "staff_bottle_fine_11_44" in positive

    # Fail-open: if the flow lost its balances (bot restart, cleared user_data)
    # the screen must still offer Collect. Hiding it on a positive place is the
    # worse failure; the qty-picker guard is the backstop.
    context.user_data["pending_bottle_collection_flow"].pop("picker_place_balances", None)
    blind = _callbacks(_edited_markup(
        _run_select_address(monkeypatch, context, "staff_bottle_addr_11_45")
    ))
    assert "staff_bottle_collect_11_45" in blind


# ---------------------------------------------------------------------------
# 3. Quantity-picker guard — branch on `< 0` BEFORE the max(0, …) clamp (H12)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_qty_guard_splits_over_returned_from_an_empty_place(monkeypatch, i18n_spy):
    """Today both collapse onto ``no_bottle_balance`` + a bare Back button.

    The negative branch must be taken before control reaches
    ``bottle_collection_qty_picker``, whose ``max(0, int(balance))`` clamp would
    otherwise render a picker with nothing on it but Cancel.
    """
    update, _ = _run_start_bottle_collection(monkeypatch, [_place_row(place_balance=-3.0)])
    assert _edited_text(update) == "staff.delivery.place_over_returned"
    assert _kwargs_for(i18n_spy, "staff.delivery.place_over_returned") == {"count": "3"}
    negative = _callbacks(_edited_markup(update))
    assert "staff_bottle_fine_11_44" in negative           # a fine is still issuable
    assert "staff_bottle_collect_11_44" not in negative
    assert not any(c.startswith("staff_bottle_qty_") for c in negative)

    # Zero keeps its own copy, but NOT a dead end: a fine is issuable at a place
    # with no empties too, so the actions stay on screen exactly as they do on
    # the negative arm.
    i18n_spy.clear()
    update, _ = _run_start_bottle_collection(monkeypatch, [_place_row(place_balance=0.0)])
    assert _edited_text(update) == "staff.delivery.no_bottle_balance"
    zero = _callbacks(_edited_markup(update))
    assert "staff_bottle_fine_11_44" in zero
    assert "staff_bottle_collect_11_44" not in zero
    assert not any(c.startswith("staff_bottle_qty_") for c in zero)

    # The POSITIVE fractional half of the same case the negative arm handles:
    # `!= 0` lets 0.5 through and the picker now labels it "(0.5)", but int()
    # truncates it to 0 here. It must not dead-end where -0.5 does not.
    i18n_spy.clear()
    update, _ = _run_start_bottle_collection(monkeypatch, [_place_row(place_balance=0.5)])
    assert _edited_text(update) == "staff.delivery.no_bottle_balance"
    fractional = _callbacks(_edited_markup(update))
    assert "staff_bottle_fine_11_44" in fractional
    assert not any(c.startswith("staff_bottle_qty_") for c in fractional)

    i18n_spy.clear()
    update, context = _run_start_bottle_collection(monkeypatch, [_place_row(place_balance=7.0)])
    positive = _callbacks(_edited_markup(update))
    assert any(c.startswith("staff_bottle_qty_11_44_") for c in positive)
    assert context.user_data["pending_bottle_collection_flow"]["balance"] == 7


# ---------------------------------------------------------------------------
# 4. Collection receipt — `remaining_balance` is the PLACE's, and unclamped
# ---------------------------------------------------------------------------


def _run_finalize_collection(monkeypatch, remaining, quantity=2):
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    handler = BottleCollectionHandler()
    update, context = _make_update_context(message_text="note")
    context.user_data["pending_bottle_collection_flow"] = {
        "customer_id": 11, "address_id": 44, "action": "collect", "quantity": quantity,
    }
    client = _AsyncClient(
        record_bottle_collection=AsyncMock(return_value=_ok({"remaining_balance": remaining}))
    )
    _patch_handler(monkeypatch, handler, mod, client)
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())
    asyncio.run(handler.receive_collection_note(update, context))
    return update


@pytest.mark.unit
def test_collection_receipt_names_an_over_returned_remainder(monkeypatch, i18n_spy):
    """``remaining_balance`` is now the place's and is NOT clamped, so a driver
    can be handed ``Remaining balance: -3`` — which reads as an error."""
    _run_finalize_collection(monkeypatch, remaining=-3.0)
    assert _kwargs_for(i18n_spy, "staff.delivery.bottle_collection_recorded_over_returned") == {
        "quantity": 2, "remaining": "3",
    }
    assert not any(c["key"] == "staff.delivery.bottle_collection_recorded" for c in i18n_spy)

    i18n_spy.clear()
    _run_finalize_collection(monkeypatch, remaining=5.0)
    assert _kwargs_for(i18n_spy, "staff.delivery.bottle_collection_recorded") == {
        "quantity": 2, "remaining": "5",
    }

    i18n_spy.clear()
    _run_finalize_collection(monkeypatch, remaining=0.0)
    assert _kwargs_for(i18n_spy, "staff.delivery.bottle_collection_recorded") == {
        "quantity": 2, "remaining": "0",
    }


# ---------------------------------------------------------------------------
# 5. Fine prompt hint — a negative place says something, it does not go silent
# ---------------------------------------------------------------------------


def _run_start_fine_after_statement(monkeypatch, place_balance):
    """Drive the PRODUCER (`show_customer_bottle_statement`) first, so the hint
    is built from a real-shaped payload rather than a pre-baked flow."""
    from staff_bot.handlers.delivery import bottle_collection as mod
    from staff_bot.utils import flow_state

    _, context, _ = _run_show_bottle_statement(
        monkeypatch,
        summary=_bottle_summary(
            addresses=[_summary_address(place_balance=place_balance)],
            cluster_scopes=[_cluster_scope(balance=place_balance)],
        ),
        place_rows=[_place_row(place_balance=place_balance)],
    )

    handler = BottleCollectionHandler()
    update, _ = _make_update_context(callback_data="staff_bottle_fine_11_44")
    _patch_handler(monkeypatch, handler, mod, _AsyncClient())
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    asyncio.run(handler.start_fine(update, context))
    return _edited_text(update)


@pytest.mark.unit
def test_fine_hint_distinguishes_over_returned_from_positive_and_zero(monkeypatch, i18n_spy):
    """The ``> 0`` filter leaves a driver fining an over-returned place with no
    context at all. Same ``{union}`` kwarg as the sibling key — renaming it
    would make ``str.format`` raise and print the raw template to the driver."""
    negative = _run_start_fine_after_statement(monkeypatch, -3.0)
    assert "staff.delivery.fine_place_over_returned_hint" in negative
    assert "staff.delivery.fine_place_union_hint" not in negative
    assert _kwargs_for(i18n_spy, "staff.delivery.fine_place_over_returned_hint") == {"union": "3"}

    i18n_spy.clear()
    positive = _run_start_fine_after_statement(monkeypatch, 7.0)
    assert "staff.delivery.fine_place_union_hint" in positive
    assert "staff.delivery.fine_place_over_returned_hint" not in positive

    i18n_spy.clear()
    zero = _run_start_fine_after_statement(monkeypatch, 0.0)
    assert "staff.delivery.fine_place_over_returned_hint" not in zero
    assert "staff.delivery.fine_place_union_hint" not in zero


# ---------------------------------------------------------------------------
# 6. At-door return prompt + the snapshot allowlist that feeds it (H10)
# ---------------------------------------------------------------------------


def _prompt_key(suggested, signed):
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    handler = StatusUpdateHandler.__new__(StatusUpdateHandler)
    context = MagicMock()
    context.user_data = {
        "current_delivery": {
            "customer_bottle_balance": suggested,
            "place_bottle_balance_signed": signed,
        }
    }
    _, message = handler._build_bottle_prompt("en", 55, context)
    return message


@pytest.mark.unit
def test_at_door_prompt_distinguishes_over_returned_from_no_record(i18n_spy):
    """Today an over-returned place is told "No empties are on record for this
    customer yet" — factually wrong: there IS a record and it is negative."""
    assert _prompt_key(0, -3.0) == "staff.delivery.bottles_return_prompt_over_returned"
    assert _kwargs_for(
        i18n_spy, "staff.delivery.bottles_return_prompt_over_returned"
    ) == {"count": "3"}

    assert _prompt_key(0, 0.0) == "staff.delivery.bottles_return_prompt_no_balance"
    assert _prompt_key(5, 5.0) == "staff.delivery.bottles_return_prompt"

    # A delivery snapshot taken before the backend field shipped must degrade to
    # today's copy, never crash.
    from staff_bot.handlers.delivery.status_update import StatusUpdateHandler

    handler = StatusUpdateHandler.__new__(StatusUpdateHandler)
    context = MagicMock()
    context.user_data = {"current_delivery": {"customer_bottle_balance": 0}}
    _, legacy = handler._build_bottle_prompt("en", 55, context)
    assert legacy == "staff.delivery.bottles_return_prompt_no_balance"


@pytest.mark.unit
def test_active_delivery_snapshot_whitelists_the_signed_place_balance(monkeypatch):
    """H10. ``current_delivery`` is an explicit ALLOWLIST and the at-door prompt
    reads only that snapshot, so a field not named there is dropped — the
    backend can emit ``place_bottle_balance_signed`` forever and the prompt will
    silently never fire."""
    from staff_bot.handlers.delivery import active_delivery as mod
    from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler

    handler = ActiveDeliveryHandler()
    update, context = _make_update_context(callback_data="staff_view_active_5")
    delivery = {
        "delivery_id": 5, "order_number": "ORD-1", "status": "arrived",
        "customer_id": 11, "customer_name": "Alice", "customer_phone": "+99890",
        "district": "Chilonzor", "address": "Office st 1", "items": [],
        "payment_method": "cash", "payment_status": "pending", "total_amount": 15000,
        "amount_collected": 0, "outstanding_amount": 15000,
        "expected_cash_to_collect": 15000, "cod_reserved_prepayment_amount": 0,
        "expected_returnable_bottles": 2,
        "customer_bottle_balance": 0.0,
        "place_bottle_balance_signed": -3.0,
    }
    client = _AsyncClient(
        get_active_deliveries=AsyncMock(return_value=_ok({"items": [delivery]}))
    )
    _patch_handler(monkeypatch, handler, mod, client)

    asyncio.run(handler.view_active_delivery(update, context))

    snapshot = context.user_data["current_delivery"]
    assert snapshot["place_bottle_balance_signed"] == -3.0
    assert snapshot["customer_bottle_balance"] == 0.0   # the clamped anchor survives


# ---------------------------------------------------------------------------
# 7. Seed-script guards — bidirectional, and the two ownership hazards
# ---------------------------------------------------------------------------


def _load(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _placeholders(value):
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


EXPECTED_PLACEHOLDERS = {
    "staff.delivery.place_over_returned": {"count"},
    "staff.delivery.fine_place_over_returned_hint": {"union"},
    "staff.delivery.bottles_return_prompt_over_returned": {"count"},
    "staff.delivery.bottle_collection_recorded_over_returned": {"quantity", "remaining"},
}


@pytest.mark.unit
def test_seed_script_side_effect_is_behind_a_main_guard():
    """``_load`` execs the module; an unguarded ``run()`` would fire a real seed
    against the dev database from a unit test."""
    src = (REPO_ROOT / "scripts" / f"{SEED_SCRIPT}.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    body = src.split('if __name__ == "__main__":')[0]
    assert "run()" not in body.replace("def run()", "")


@pytest.mark.unit
def test_seed_script_ships_all_four_keys_trilingually():
    """A missing ``en`` row is the dangerous one: ``DEFAULT_LANGUAGE`` is ``uz``,
    so an English-canonical column with no ``en`` row renders Uzbek."""
    mod = _load(SEED_SCRIPT)
    assert mod.CATEGORY == "staff_bot"
    assert set(mod.KEYS) == OVER_RETURNED_KEYS
    for key, langs in mod.KEYS.items():
        assert set(langs) == {"en", "uz", "ru"}, key
        for lang, value in langs.items():
            assert value.strip(), f"{key}:{lang}"


@pytest.mark.unit
def test_seed_script_placeholders_are_identical_in_every_language():
    """A dropped placeholder is a number the driver never sees:
    ``staff_bot/i18n.py`` catches the ``KeyError`` and renders the raw template."""
    mod = _load(SEED_SCRIPT)
    for key, langs in mod.KEYS.items():
        for lang, value in langs.items():
            assert _placeholders(value) == EXPECTED_PLACEHOLDERS[key], f"{key}:{lang}"


@pytest.mark.unit
def test_seed_script_keys_all_have_call_sites():
    """Mirrors ``StaffI18n._extract_literal_staff_keys`` — the scan that drives
    ``/health``. Bidirectional: a seeded key nobody reads is dead weight, a read
    key nobody seeds takes the bot to 503."""
    mod = _load(SEED_SCRIPT)
    pattern = re.compile(r"""i18n\.get\(\s*(['"])(staff\.[^'"]+)\1\s*[,)]""")
    used = set()
    for path in (REPO_ROOT / "staff_bot").rglob("*.py"):
        for _, key in pattern.findall(path.read_text(encoding="utf-8")):
            used.add(key)

    assert set(mod.KEYS) <= used, f"seeded but never read: {sorted(set(mod.KEYS) - used)}"
    assert OVER_RETURNED_KEYS <= used


@pytest.mark.unit
def test_seed_script_owns_these_keys_alone():
    """H5 + H3 — one key, one owner.

    ``scripts/seed_staff_translations.py`` upserts a curated value for any suffix
    it knows, so a shared suffix is a seed race: whichever runs last wins, and
    the humanised guess drops the placeholders. And these four cannot live in
    ``seed_place_group_staff_translations.py`` either — its guards assert an
    exact 8-key set and index ``EXPECTED_PLACEHOLDERS`` by key.
    """
    mod = _load(SEED_SCRIPT)
    main_src = (REPO_ROOT / "scripts" / "seed_staff_translations.py").read_text(encoding="utf-8")
    for key in mod.KEYS:
        suffix = key.split(".", 2)[2]
        assert f'"{suffix}"' not in main_src, f"{key} already curated in seed_staff_translations"
        assert f"'{suffix}'" not in main_src, f"{key} already curated in seed_staff_translations"

    place_group = _load("seed_place_group_staff_translations")
    assert not (OVER_RETURNED_KEYS & set(place_group.KEYS))
