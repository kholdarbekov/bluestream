"""L3 bot half — the per-INTENT idempotency token — plus RULING 2's
ambiguous-failure driver warning, for the staff-bot collection and fine flows.

Two distinct properties are pinned here and they fail in OPPOSITE directions:

1. **One token per INTENT, not per transmission.** The token is minted when the
   driver reaches the confirm step (`pick_collection_qty` for a collection,
   `receive_fine_amount` for a fine — the note message IS the fine's confirm),
   carried in the flow dict, and sent on every submit of that intent. Minting at
   submit time would buy nothing; a token that OUTLIVED its intent would be
   worse than a duplicate, because the backend would silently swallow a second
   genuine collection at HTTP 200 with no ledger row — an invisible loss of the
   customer's bottles. Hence the "dies with the flow" and "a fresh flow mints a
   fresh token" pins below.

2. **The driver is told when a write MAY have landed.** After
   `.superpowers/sdd/2026-08-03-retry-safety/RULINGS.md` RULING 1 the transport
   no longer re-POSTs an ambiguous failure, so the driver sees an error after
   ONE send and is more likely to redo the flow by hand — which mints a NEW
   token that L3 cannot dedup. `TRANSPORT_AMBIGUOUS` (stamped by
   `staff_bot/api_client.py`'s terminal `APIResponse`) is the only signal that
   means "this may already be recorded", and it must be shown for that phase
   ONLY: a connect-phase failure was provably never delivered, so the warning
   would be a lie there.

Copy assertions stub `i18n.get` to echo the key, because staff_bot humanises a
missing key (`staff_bot/i18n.py`) and these tests must not depend on the seed.
"""

import asyncio
import importlib.util
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.api_client import TRANSPORT_AMBIGUOUS_ERROR_CODE
from staff_bot.handlers.delivery import bottle_collection as mod
from staff_bot.handlers.delivery.bottle_collection import BottleCollectionHandler
from staff_bot.utils import flow_state

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed_staff_translations.py"

FLOW_KEY = "pending_bottle_collection_flow"
TOKEN_FIELD = "idempotency_key"
MAYBE_RECORDED_KEY = "staff.error.api.maybe_recorded"
HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")

# The server-side validator is `\A[A-Za-z0-9_-]{8,64}\Z` applied with
# `fullmatch` (business_app/services/bottle_tracking_service.py). A token the
# bot mints must satisfy it, or every driver submission 400s with
# BOTTLE_IDEMPOTENCY_KEY_INVALID.
SERVER_TOKEN_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _context(flow=None):
    context = MagicMock()
    context.user_data = {
        "language": "en",
        "authenticated": True,
        "staff_roles": ["delivery_driver"],
    }
    if flow is not None:
        context.user_data[FLOW_KEY] = dict(flow)
    context.bot = MagicMock()
    return context


def _cb_update(data=None):
    update = MagicMock()
    update.effective_user = MagicMock(id=999)
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    return update


def _msg_update(text):
    update = MagicMock()
    update.effective_user = MagicMock(id=999)
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


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


def _resp(success=True, data=None, error=None, error_code=None, status_code=None):
    return MagicMock(
        success=success,
        data={} if data is None else data,
        error=error,
        error_code=error_code,
        status_code=status_code,
    )


def _collect_client(response=None):
    return _AsyncClient(
        record_bottle_collection=AsyncMock(
            return_value=response or _resp(data={"remaining_balance": 3})
        )
    )


def _fine_client(response=None):
    return _AsyncClient(
        create_bottle_fine=AsyncMock(return_value=response or _resp(data={"id": 1}))
    )


def _patch(monkeypatch, handler, client):
    monkeypatch.setattr(mod, "api_client", client)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())


def _echo_i18n(monkeypatch):
    """Make ``i18n.get`` echo the key so copy assertions name a KEY, not text."""
    monkeypatch.setattr(mod.i18n, "get", lambda key, language=None, *a, **k: key)


def _notified(update):
    """The single string the driver was shown, whichever channel was used."""
    if update.callback_query is not None:
        call = update.callback_query.answer.call_args
    else:
        call = update.message.reply_text.call_args
    assert call is not None, "the driver was told nothing at all"
    return call.args[0]


def _posted_collection(client):
    return client.client.record_bottle_collection.await_args.args[1]


def _posted_fine(client):
    return client.client.create_bottle_fine.await_args.args[1]


_COLLECT_FLOW = {"customer_id": 11, "address_id": 44, "action": "collect", "balance": 9}
_FINE_FLOW = {"customer_id": 11, "address_id": 44, "action": "fine", "fine_quantity": 2}


# ---------------------------------------------------------------------------
# A. Mint sites — ONE token per intent, at the confirm step
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pick_collection_qty_mints_the_intent_token(monkeypatch):
    """The moment the confirm keyboard goes up, the decision exists."""
    handler = BottleCollectionHandler()
    context = _context(_COLLECT_FLOW)
    update = _cb_update("staff_bottle_qty_11_44_5")
    _patch(monkeypatch, handler, _collect_client())

    asyncio.run(handler.pick_collection_qty(update, context))

    token = context.user_data[FLOW_KEY][TOKEN_FIELD]
    assert HEX32.fullmatch(token), token
    assert SERVER_TOKEN_PATTERN.fullmatch(token), (
        "the backend validator would reject this token with "
        "BOTTLE_IDEMPOTENCY_KEY_INVALID"
    )


@pytest.mark.unit
def test_receive_fine_amount_mints_the_intent_token(monkeypatch):
    """A fine has no confirm BUTTON — the note message is the confirm — so the
    amount step is the last state before the money-carrying POST."""
    handler = BottleCollectionHandler()
    context = _context(_FINE_FLOW)
    update = _msg_update("50000")
    _patch(monkeypatch, handler, _fine_client())

    asyncio.run(handler.receive_fine_amount(update, context))

    token = context.user_data[FLOW_KEY][TOKEN_FIELD]
    assert HEX32.fullmatch(token), token
    assert SERVER_TOKEN_PATTERN.fullmatch(token)


@pytest.mark.unit
def test_a_rejected_fine_amount_mints_nothing(monkeypatch):
    """The intent does not exist until the amount is accepted."""
    handler = BottleCollectionHandler()
    context = _context(_FINE_FLOW)
    _patch(monkeypatch, handler, _fine_client())

    asyncio.run(handler.receive_fine_amount(_msg_update("not-a-number"), context))

    assert TOKEN_FIELD not in context.user_data[FLOW_KEY]


@pytest.mark.unit
def test_finalizing_a_collection_does_not_mint_a_token(monkeypatch):
    """PER-INTENT, not per-tap. A token minted at submit time is a fresh key on
    every transmission and dedups exactly nothing — and it would also break the
    backward-compatible body a flow without a token must still post."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5})
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    assert TOKEN_FIELD not in _posted_collection(client)


@pytest.mark.unit
def test_submitting_a_fine_does_not_mint_a_token(monkeypatch):
    """Same rule on the money-carrying half."""
    handler = BottleCollectionHandler()
    context = _context({**_FINE_FLOW, "fine_amount": 50000.0})
    client = _fine_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.receive_fine_note(_msg_update("late"), context))

    assert TOKEN_FIELD not in _posted_fine(client)


# ---------------------------------------------------------------------------
# B. Send sites — conditional, so a token-less flow posts today's exact body
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_collection_body_carries_the_flows_token(monkeypatch):
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "a" * 32})
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    assert _posted_collection(client) == {
        "customer_id": 11,
        "address_id": 44,
        "quantity": 5,
        "notes": "note",
        TOKEN_FIELD: "a" * 32,
    }


@pytest.mark.unit
def test_a_collection_flow_without_a_token_posts_the_pre_existing_body(monkeypatch):
    """The field is added CONDITIONALLY. A flow dict minted by an older bot
    process, or by any future caller that does not mint, must post exactly the
    four route keys — the backend then takes its un-keyed path."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5})
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    assert set(_posted_collection(client)) == {
        "customer_id", "address_id", "quantity", "notes",
    }


@pytest.mark.unit
def test_the_fine_body_carries_the_flows_token():
    body = BottleCollectionHandler._build_fine_payload(
        {"customer_id": 7, "address_id": 44, TOKEN_FIELD: "b" * 32},
        2, 50000, "two missing",
    )
    assert body == {
        "customer_id": 7,
        "address_id": 44,
        "quantity": 2,
        "fine_amount": 50000,
        "notes": "two missing",
        TOKEN_FIELD: "b" * 32,
    }


@pytest.mark.unit
def test_a_fine_flow_without_a_token_posts_the_pre_existing_body():
    """Pins the same conditional on the fine half — this is the exact call
    `tests/unit/test_staff_bot_place_surfaces.py` makes with strict equality."""
    body = BottleCollectionHandler._build_fine_payload(
        {"customer_id": 7, "address_id": 44}, 2, 50000, "two missing"
    )
    assert set(body) == {
        "customer_id", "address_id", "quantity", "fine_amount", "notes",
    }


@pytest.mark.unit
def test_the_token_minted_at_the_picker_is_the_one_posted(monkeypatch):
    """End-to-end on the real handler chain: mint once, send that one."""
    handler = BottleCollectionHandler()
    context = _context(_COLLECT_FLOW)
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.pick_collection_qty(_cb_update("staff_bottle_qty_11_44_5"), context))
    minted = context.user_data[FLOW_KEY][TOKEN_FIELD]
    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    assert _posted_collection(client)[TOKEN_FIELD] == minted


@pytest.mark.unit
def test_the_token_minted_at_the_amount_step_is_the_one_posted(monkeypatch):
    handler = BottleCollectionHandler()
    context = _context(_FINE_FLOW)
    client = _fine_client()
    _patch(monkeypatch, handler, client)

    asyncio.run(handler.receive_fine_amount(_msg_update("50000"), context))
    minted = context.user_data[FLOW_KEY][TOKEN_FIELD]
    asyncio.run(handler.receive_fine_note(_msg_update("late"), context))

    assert _posted_fine(client)[TOKEN_FIELD] == minted


# ---------------------------------------------------------------------------
# C. The token must never outlive its intent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        _resp(data={"remaining_balance": 3}),
        _resp(success=False, error="boom", status_code=500),
        _resp(success=False, error="Request failed after retries",
              error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE),
    ],
    ids=["success", "hard-failure", "ambiguous-failure"],
)
def test_a_submitted_collection_token_dies_with_its_flow(monkeypatch, response):
    """`_finalize_collection` clears in a `finally`, so there is NO submit
    outcome — success, refusal or crash — that leaves the token armed. A token
    that survived its submit would let the driver's NEXT genuine collection be
    swallowed by the backend's dedup at HTTP 200, with no ledger row and no
    session-tally bump: an invisible loss, strictly worse than a duplicate."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "c" * 32})
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    assert FLOW_KEY not in context.user_data


@pytest.mark.unit
@pytest.mark.parametrize(
    "response",
    [
        _resp(data={"id": 1}),
        _resp(success=False, error="boom", status_code=500),
        _resp(success=False, error="Request failed after retries",
              error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE),
    ],
    ids=["success", "hard-failure", "ambiguous-failure"],
)
def test_a_submitted_fine_token_dies_with_its_flow(monkeypatch, response):
    handler = BottleCollectionHandler()
    context = _context({**_FINE_FLOW, "fine_amount": 50000.0, TOKEN_FIELD: "d" * 32})
    _patch(monkeypatch, handler, _fine_client(response))
    _echo_i18n(monkeypatch)

    asyncio.run(handler.receive_fine_note(_msg_update("late"), context))

    assert FLOW_KEY not in context.user_data


@pytest.mark.unit
def test_begin_flow_does_not_carry_a_token_into_a_new_intent():
    """`_begin_flow` replaces the dict wholesale and carries over exactly two
    read-only lookup maps. Adding the token to that allow-list would make one
    key serve two decisions — the invisible-loss failure above."""
    context = _context({
        "customer_id": 1, "address_id": 2, "action": "collect", "quantity": 5,
        TOKEN_FIELD: "stale-token-value",
        "place_balances": {2: 4.0},
        "picker_place_balances": {2: 4.0},
    })

    flow = BottleCollectionHandler._begin_flow(
        context, customer_id=1, address_id=2, action="collect"
    )

    assert TOKEN_FIELD not in flow
    assert "quantity" not in flow
    assert flow["place_balances"] == {2: 4.0}
    assert flow["picker_place_balances"] == {2: 4.0}
    assert context.user_data[FLOW_KEY] is flow


@pytest.mark.unit
def test_two_consecutive_collections_carry_two_different_tokens(monkeypatch):
    """A genuinely NEW collection is a new flow, so it lands. The token
    collapses duplicate DELIVERIES of one body, never two real decisions."""
    handler = BottleCollectionHandler()
    context = _context()
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    for _ in range(2):
        BottleCollectionHandler._begin_flow(
            context, customer_id=11, address_id=44, action="collect"
        )
        asyncio.run(
            handler.pick_collection_qty(_cb_update("staff_bottle_qty_11_44_5"), context)
        )
        asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    posted = [c.args[1] for c in client.client.record_bottle_collection.await_args_list]
    assert len(posted) == 2
    assert len({p[TOKEN_FIELD] for p in posted}) == 2
    assert FLOW_KEY not in context.user_data


@pytest.mark.unit
def test_re_picking_a_quantity_re_mints_the_token_within_one_flow(monkeypatch):
    """Changing the quantity is a NEW decision, so it needs a NEW token.

    `test_two_consecutive_collections_carry_two_different_tokens` calls
    `_begin_flow` before each pick, which wipes the flow dict — so it would stay
    green even if the mint became `flow.setdefault('idempotency_key', ...)`.
    This drives two picks on ONE flow, which is what a driver does when they tap
    5, realise it is wrong, and tap 3.

    Under a `setdefault` refactor the second collection would post the FIRST
    token paired with quantity 3. The backend's `_assert_replay_matches_*` guard
    would spot the mismatch and answer **409 BOTTLE_IDEMPOTENCY_KEY_REUSED** —
    at a customer's door, with the whole suite green. `_new_intent_token`'s own
    docstring ("minted when the driver reaches the confirm step") reads like an
    invitation to that refactor, which is exactly why this is pinned.
    """
    handler = BottleCollectionHandler()
    context = _context()
    client = _collect_client()
    _patch(monkeypatch, handler, client)

    BottleCollectionHandler._begin_flow(
        context, customer_id=11, address_id=44, action="collect"
    )

    asyncio.run(handler.pick_collection_qty(_cb_update("staff_bottle_qty_11_44_5"), context))
    first_token = context.user_data[FLOW_KEY][TOKEN_FIELD]

    # The driver corrects themselves — same flow, no `_begin_flow`.
    asyncio.run(handler.pick_collection_qty(_cb_update("staff_bottle_qty_11_44_3"), context))
    second_token = context.user_data[FLOW_KEY][TOKEN_FIELD]

    assert re.fullmatch(r"[0-9a-f]{32}", first_token)
    assert re.fullmatch(r"[0-9a-f]{32}", second_token)
    assert second_token != first_token, (
        "a re-picked quantity kept the first token — a setdefault-style mint "
        "would 409 at the door"
    )

    asyncio.run(handler.receive_collection_note(_msg_update("note"), context))

    posted = [c.args[1] for c in client.client.record_bottle_collection.await_args_list]
    assert len(posted) == 1
    # The token that ships must be paired with the quantity it was minted for.
    assert posted[0][TOKEN_FIELD] == second_token
    assert posted[0]["quantity"] == 3


@pytest.mark.unit
def test_the_flow_key_is_in_the_clear_pending_flows_ssot():
    """`flow_state.clear_pending_flows` must reach the token: /start, the main
    menu, the cash hub and the cancel button all go through it."""
    assert FLOW_KEY in flow_state.PENDING_FLOW_USER_DATA_KEYS


# ---------------------------------------------------------------------------
# D. RULING 2 — the ambiguous-failure warning
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_ambiguous_collection_failure_warns_that_it_may_be_recorded(monkeypatch):
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "e" * 32})
    response = _resp(success=False, error="Request failed after retries",
                     error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE)
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("note")
    asyncio.run(handler.receive_collection_note(update, context))

    assert MAYBE_RECORDED_KEY in _notified(update)


@pytest.mark.unit
def test_an_ambiguous_fine_failure_warns_that_it_may_be_recorded(monkeypatch):
    handler = BottleCollectionHandler()
    context = _context({**_FINE_FLOW, "fine_amount": 50000.0, TOKEN_FIELD: "f" * 32})
    response = _resp(success=False, error="Request failed after retries",
                     error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE)
    _patch(monkeypatch, handler, _fine_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("late")
    asyncio.run(handler.receive_fine_note(update, context))

    assert MAYBE_RECORDED_KEY in _notified(update)


@pytest.mark.unit
def test_the_ambiguous_warning_reaches_a_button_driven_collection(monkeypatch):
    """`save_collection_no_note` submits from a callback query, so the warning
    has to survive the alert-popup channel too."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "e" * 32})
    response = _resp(success=False, error="Request failed after retries",
                     error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE)
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    update = _cb_update("staff_bottle_collect_save_no_note")
    asyncio.run(handler.save_collection_no_note(update, context))

    assert MAYBE_RECORDED_KEY in _notified(update)


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code, status_code",
    [
        (None, None),                     # connect-phase exhaustion: never delivered
        ("BOTTLE_SCOPE_LOCK_TIMEOUT", 409),
        ("BOTTLE_IDEMPOTENCY_KEY_REUSED", 409),
        ("BOTTLE_SCOPE_MEMBERSHIP_REQUIRED", 403),
    ],
)
def test_a_non_ambiguous_collection_failure_never_claims_it_may_be_recorded(
    monkeypatch, error_code, status_code
):
    """The warning is scoped to the AMBIGUOUS phase alone. A connect-phase
    failure provably never reached the backend and a named 4xx is a deterministic
    refusal — telling the driver either "may already be recorded" would send them
    hunting through the bottle statement for a row that does not exist, and would
    teach them to distrust the warning when it is real."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "e" * 32})
    response = _resp(success=False, error="Request failed after retries",
                     error_code=error_code, status_code=status_code)
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("note")
    asyncio.run(handler.receive_collection_note(update, context))

    assert MAYBE_RECORDED_KEY not in _notified(update)


@pytest.mark.unit
@pytest.mark.parametrize(
    "error_code, status_code",
    [
        (None, None),
        ("BOTTLE_SCOPE_LOCK_TIMEOUT", 409),
        ("BOTTLE_IDEMPOTENCY_KEY_REUSED", 409),
    ],
)
def test_a_non_ambiguous_fine_failure_never_claims_it_may_be_recorded(
    monkeypatch, error_code, status_code
):
    handler = BottleCollectionHandler()
    context = _context({**_FINE_FLOW, "fine_amount": 50000.0, TOKEN_FIELD: "f" * 32})
    response = _resp(success=False, error="Request failed after retries",
                     error_code=error_code, status_code=status_code)
    _patch(monkeypatch, handler, _fine_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("late")
    asyncio.run(handler.receive_fine_note(update, context))

    assert MAYBE_RECORDED_KEY not in _notified(update)


@pytest.mark.unit
def test_a_named_backend_error_still_renders_its_own_copy(monkeypatch):
    """The warning must not hijack the mapped copy for a real backend refusal —
    `BOTTLE_SCOPE_LOCK_TIMEOUT` has its own "nothing was saved, retry shortly"
    text and that is the opposite advice."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "e" * 32})
    response = _resp(success=False, error="conflict",
                     error_code="BOTTLE_SCOPE_LOCK_TIMEOUT", status_code=409)
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("note")
    asyncio.run(handler.receive_collection_note(update, context))

    assert "staff.error.api.scope_busy" in _notified(update)


@pytest.mark.unit
def test_an_ambiguous_success_is_impossible_so_the_receipt_still_wins(monkeypatch):
    """`TRANSPORT_AMBIGUOUS` only ever rides a `success=False` response
    (staff_bot/api_client.py stamps it on the terminal give-up return). Guard
    against a future refactor that starts warning on top of a receipt."""
    handler = BottleCollectionHandler()
    context = _context({**_COLLECT_FLOW, "quantity": 5, TOKEN_FIELD: "e" * 32})
    response = _resp(success=True, data={"remaining_balance": 3},
                     error_code=TRANSPORT_AMBIGUOUS_ERROR_CODE)
    _patch(monkeypatch, handler, _collect_client(response))
    _echo_i18n(monkeypatch)

    update = _msg_update("note")
    asyncio.run(handler.receive_collection_note(update, context))

    shown = update.message.reply_text.call_args.args[0]
    assert MAYBE_RECORDED_KEY not in shown


# ---------------------------------------------------------------------------
# E. The translation key itself
# ---------------------------------------------------------------------------


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_the_warning_key_is_seeded_trilingually():
    """staff_bot convention: category='staff_bot' with a DOTTED key, curated in
    `scripts/seed_staff_translations.py`. This is NOT the admin-UI namespace
    (category='ui_staff', BARE keys). An unseeded key would degrade to
    staff_bot's humanised last segment — "Maybe recorded" — in all three
    languages, which says nothing useful at a customer's door."""
    curated = _seed_module().STAFF_TRANSLATIONS
    assert MAYBE_RECORDED_KEY in curated
    values = curated[MAYBE_RECORDED_KEY]
    assert set(values) == {"en", "uz", "ru"}
    for language, value in values.items():
        assert value.strip(), language
        # No kwargs are passed at the call site, so a placeholder would render
        # verbatim (staff_bot/i18n.py only formats when args/kwargs are given).
        assert "{" not in value and "}" not in value, language


@pytest.mark.unit
def test_the_warning_fits_a_telegram_callback_alert():
    """`BaseHandler._notify_user` answers a callback query with `show_alert`,
    and Telegram caps that text at 200 characters. Over the cap the alert is
    rejected and the driver silently falls back to a chat reply — which the
    button-driven "Save without note" path would hit every time."""
    curated = _seed_module().STAFF_TRANSLATIONS[MAYBE_RECORDED_KEY]
    for language, value in curated.items():
        assert len(f"⚠️ {value}") <= 200, (language, len(value))
