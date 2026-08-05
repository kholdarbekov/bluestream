"""🔴 SIXTH INSTANCE OF THE SHOW-VS-SETTLE SPLIT — the post-collection RECEIPT.

``CashCollectionHandler._format_statement`` and ``_collect_offer`` were made one
calculation (fifth instance, ``tests/unit/test_cod_collect_ceiling_row_pin.py``)
and the docstring on ``_format_statement`` says in so many words that the raw
per-account ``total_outstanding_amount`` is *"deliberately NOT rendered: it
counts PENDING orders the allocation engine cannot settle"*.

``receive_collection_note`` — the very next screen, the one the driver reads
*after* handing back change — built its ``remaining=`` figure from exactly that
key:

    remaining_outstanding = float(statement['total_outstanding_amount'] or 0)

So the receipt re-introduced, one message later, the number every other surface
had been fixed to stop showing. Measured on the canonical A6 rows plus one
PENDING order the engine cannot touch, after a real full 45 000 collection: the
receipt carried **remaining = 70 000** while the next "Collect full" offers
**0**.

WHY THE TESTS SUPPLY THEIR OWN COPY FOR ``staff.delivery.cod_collection_recorded``
The shipped seed for that key is a bare *"Collection recorded successfully."* —
it contains neither ``{amount}`` nor ``{remaining}``, so ``str.format`` silently
discards both kwargs and TODAY the driver reads no money at all. That makes the
defect LATENT rather than live: the wrong figure is computed and handed to the
renderer, and it ships the day anyone adds ``{remaining}`` to the copy (which
the kwargs plainly invite). The rendering tests therefore install a catalog whose
template DOES interpolate both placeholders — that is the contract the handler
declares by passing them — and assert the money in the resulting string.
``test_shipped_copy_currently_drops_the_money`` pins the latency itself so the
gap between "computed" and "shown" cannot be forgotten.
"""

import asyncio
import re
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.staff_service import StaffService
from shared import business_config
from shared.enums import OrderStatus
from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler
from staff_bot.i18n import Translation
from staff_bot.utils.formatters import format_currency
from tests.unit._scope_money_helpers import (
    delivered_cod_order,
    make_address,
    make_place_group,
    make_user,
)


RECEIPT_KEY = "staff.delivery.cod_collection_recorded"

# A template that USES both kwargs the handler passes. Deliberately not the
# shipped copy — see the module docstring.
RENDERING_CATALOG = {
    lang: {
        "staff.currency.uzs": "UZS",
        RECEIPT_KEY: "Collected {amount}. Remaining {remaining}.",
    }
    for lang in ("en", "uz", "ru")
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self):
        self.user_data = {"language": "en", "authenticated": True,
                          "staff_roles": ["delivery_driver"]}


@pytest.fixture(autouse=True)
def _gate(app, monkeypatch):
    """Both halves of the place gate ON; the Flask mirror restored afterwards
    (``app`` is session-scoped, so a bare assignment leaks across tests)."""
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")
    app.config["PLACE_COD_COLLECTION_ENABLED"] = True
    monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", True)
    yield
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


@pytest.fixture
def rendering_i18n(monkeypatch):
    """Point the REAL ``staff_bot`` i18n singleton at :data:`RENDERING_CATALOG`.

    ``staff_bot.utils.formatters`` and the handler module both bind the same
    singleton object, so one patch covers ``format_currency`` and the receipt
    template together.
    """
    from staff_bot import i18n as i18n_module

    instance = Translation()
    instance.translations = {lang: dict(rows) for lang, rows in RENDERING_CATALOG.items()}
    monkeypatch.setattr(i18n_module.i18n, "translations", instance.translations)
    return instance


@pytest.fixture
def office(db):
    """The canonical A6 scenario PLUS one PENDING order.

    Alice  10 000 delivered at an ungrouped home
           15 000 delivered at office G
           70 000 **PENDING** (never delivered)
    Bob    20 000 delivered at office G

    The PENDING order is the whole point: the allocation engine's candidate
    rings are DELIVERED-only (``cash_collection_service.py:183-196``), so cash
    offered against it settles nothing. It inflates the per-account
    ``total_outstanding_amount`` to 95 000 while the collectible offer stays
    45 000 — which is exactly the gap the receipt was carrying.
    """
    alice, bob, admin = make_user(db), make_user(db), make_user(db)
    alice_home = make_address(db, alice)               # UNGROUPED
    alice_desk, bob_desk = make_address(db, alice), make_address(db, bob)
    group = make_place_group(db, alice_desk, bob_desk)
    delivered_cod_order(db, alice, address=alice_home, total=Decimal("10000.00"))
    delivered_cod_order(db, alice, address=alice_desk, total=Decimal("15000.00"))
    delivered_cod_order(db, bob, address=bob_desk, total=Decimal("20000.00"))
    delivered_cod_order(
        db, alice, address=alice_home, total=Decimal("70000.00"),
        status=OrderStatus.PENDING,
    )
    return {"alice": alice, "bob": bob, "admin": admin, "group": group,
            "alice_home": alice_home, "alice_desk": alice_desk, "bob_desk": bob_desk}


def _served_statement(user_id):
    """The payload ``GET /staff/customers/<id>/cod-statement`` actually serves."""
    return StaffService().get_customer_cod_statement_for_staff(user_id)


def _next_offer(user_id):
    """What the NEXT collect flow would offer, from the handler's own one
    decision — never re-derived here."""
    return CashCollectionHandler._collect_offer(_served_statement(user_id))[1]


def _drive_collection(monkeypatch, db, admin_id, user_id, amount):
    """Run the WHOLE standalone collection through the REAL handlers and return
    ``(rendered_receipt, receipt_kwargs)``.

    ``record_cash_collection`` is not stubbed to a canned success: it replays the
    posted payload through the REAL engine, so the statement the receipt then
    fetches is composed from rows the collection actually moved. A canned mock
    would leave the debt untouched and the defect would be invisible.
    """
    from staff_bot.handlers.delivery import cash_collection as mod
    from staff_bot.utils import flow_state

    async def _record(_token, payload):
        CashCollectionService().post_collection(
            customer_id=payload["customer_id"],
            amount=Decimal(str(payload["amount"])),
            source=payload["source"],
            recorded_by_user_id=admin_id,
            delivery_address_id=payload.get("delivery_address_id"),
            notes=payload.get("notes"),
        )
        db.session.commit()
        return MagicMock(success=True, data={"cash_collection_event": {"id": 1}})

    async def _statement(*_a, **_k):
        # Composed fresh on EVERY call, so the post-collection fetch inside
        # `receive_collection_note` sees the money that just moved.
        return MagicMock(success=True, data=_served_statement(user_id))

    class _AsyncClient:
        def __init__(self):
            self.client = MagicMock()
            self.client.get_customer_cod_statement = AsyncMock(side_effect=_statement)
            self.client.record_cash_collection = AsyncMock(side_effect=_record)

        async def __aenter__(self):
            return self.client

        async def __aexit__(self, *_):
            return False

    receipt_kwargs = {}
    real_get = mod.i18n.get

    def _spy_get(key, language=None, *args, **kwargs):
        if key == RECEIPT_KEY:
            receipt_kwargs.update(kwargs)
        return real_get(key, language, *args, **kwargs)

    handler = CashCollectionHandler()
    context = _Ctx()
    context.bot = MagicMock()
    monkeypatch.setattr(mod, "api_client", _AsyncClient())
    monkeypatch.setattr(mod.i18n, "get", _spy_get)
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
        _update(data=f"staff_cod_collect_custom_{user_id}"), context))
    state = asyncio.run(handler.receive_collection_amount(
        _update(text=str(amount)), context))
    if state == mod.COLLECTION_OVERPAYMENT_CONFIRM:
        asyncio.run(handler.confirm_overpayment_collection(
            _update(data="staff_cod_confirm_overpay_yes"), context))
    note_update = _update(text="handed over at the office")
    asyncio.run(handler.receive_collection_note(note_update, context))

    call = note_update.message.reply_text.call_args
    rendered = call.args[0] if call.args else call.kwargs["text"]
    return rendered, receipt_kwargs


def _shows(text, amount, language="en"):
    """Is ``amount`` rendered as money anywhere on this screen?

    Anchored on a non-digit boundary so ``format_currency(0)`` ("0 UZS") is not
    matched inside ``"45,000 UZS"``.
    """
    token = format_currency(amount, language=language)
    return re.search(r"(?<![\d,])" + re.escape(token), text) is not None


# ---------------------------------------------------------------------------
# Guard rails — the fixture must actually reproduce the divergence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_fixture_really_has_an_unsettleable_pending_debt(app, db, office):
    """If the PENDING order stopped inflating the raw headline, every assertion
    below would pass for the wrong reason."""
    served = _served_statement(office["alice"].id)
    assert served["total_outstanding_amount"] == 95000.0, (
        "25 000 of her own delivered debt + the 70 000 PENDING order"
    )
    assert CashCollectionHandler._collect_offer(served)[1] == 45000.0


@pytest.mark.unit
def test_shipped_copy_currently_drops_the_money(app, db, office):
    """The shipped ``staff.delivery.cod_collection_recorded`` interpolates
    neither kwarg, so the wrong figure is computed but not yet displayed.

    This is why the defect went unnoticed and why it is a landmine rather than a
    live lie: adding ``{remaining}`` to the copy — the obvious next edit, since
    the handler already passes it — ships the wrong number that same day. If the
    copy gains a placeholder, this test fails and the rendering tests below take
    over as the live guarantee.
    """
    from scripts.seed_staff_translations import STAFF_TRANSLATIONS

    shipped = STAFF_TRANSLATIONS[RECEIPT_KEY]
    for language, copy in shipped.items():
        assert "{remaining}" not in copy, (
            f"{language} copy now renders 'remaining' — the rendering tests in "
            "this file are the live guarantee; delete this one."
        )


# ---------------------------------------------------------------------------
# 🔴 THE PIN — the receipt's remaining IS the next collect offer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_receipt_remaining_equals_the_next_collect_offer(
    app, db, monkeypatch, office, rendering_i18n
):
    """🔴 THE INVARIANT. Do not delete this test.

    After a full 45 000 collection there is nothing left to collect. The receipt
    must say so. It carried 70 000 — the PENDING order — which reads to the
    driver as "go back and get more" over money no flow will take.
    """
    receipt, kwargs = _drive_collection(
        monkeypatch, db, office["admin"].id, office["alice"].id, 45000
    )

    remaining_next_offer = _next_offer(office["alice"].id)
    assert remaining_next_offer == 0.0, "the 45 000 collection settled everything collectible"

    assert _shows(receipt, 45000.0), receipt
    assert _shows(receipt, remaining_next_offer), receipt
    assert not _shows(receipt, 70000.0), (
        f"the receipt shows the PENDING order as still-owed money: {receipt!r}"
    )
    assert not _shows(receipt, 95000.0), receipt
    # And the figure handed to the renderer is that same one decision.
    assert kwargs["remaining"] == format_currency(remaining_next_offer, language="en")


@pytest.mark.unit
def test_partial_collection_receipt_equals_the_next_collect_offer(
    app, db, monkeypatch, office, rendering_i18n
):
    """The relation, not just the zero case: collect 20 000 of 45 000 and the
    receipt must state the 25 000 the next offer will actually make."""
    receipt, kwargs = _drive_collection(
        monkeypatch, db, office["admin"].id, office["alice"].id, 20000
    )

    remaining_next_offer = _next_offer(office["alice"].id)
    assert remaining_next_offer == 25000.0

    assert _shows(receipt, 20000.0), receipt
    assert _shows(receipt, 25000.0), receipt
    assert not _shows(receipt, 70000.0), receipt
    assert not _shows(receipt, 75000.0), receipt   # 95 000 − 20 000, the old figure
    assert kwargs["remaining"] == format_currency(remaining_next_offer, language="en")


@pytest.mark.unit
def test_the_coworkers_receipt_obeys_the_same_rule(
    app, db, monkeypatch, office, rendering_i18n
):
    """Generalisation, so an edit that keeps Alice right and Bob wrong still
    fails. Bob's raw per-account total (20 000) and his 35 000 offer straddle
    each other in the opposite direction to Alice's."""
    receipt, kwargs = _drive_collection(
        monkeypatch, db, office["admin"].id, office["bob"].id, 10000
    )

    remaining_next_offer = _next_offer(office["bob"].id)
    assert remaining_next_offer == 25000.0, "35 000 offer less the 10 000 just taken"

    assert _shows(receipt, 10000.0), receipt
    assert _shows(receipt, remaining_next_offer), receipt
    assert kwargs["remaining"] == format_currency(remaining_next_offer, language="en")
