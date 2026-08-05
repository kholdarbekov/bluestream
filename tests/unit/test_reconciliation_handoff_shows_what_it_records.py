"""🔴 EIGHTH INSTANCE OF THE SHOW-VS-SETTLE SPLIT — the driver's cash handoff.

THE DEFECT (measured, `.superpowers/sdd/2026-08-05-e2e-coverage/
sweep-as-test-report.md` §3.2)

    | SHOWN    | `expected_on_hand - cumulative_declared`, CLAMPED at 0 |
    |          | `driver_reconciliation_service.py::_serialize_session` |
    | POSTED   | `{}` — no amount at all — `status_update.py`           |
    | RECORDED | `expected_on_hand - prior_declared`, UNCLAMPED, after  |
    |          | `refresh_expected_cash` re-sums live collection events |

Two expressions, and between them a gap in wall-clock time: the driver reads the
screen, one delivery completes and stamps a COD collection against the same open
session, the driver taps. **Shown 120,000. Recorded 150,000.** A cash-custody
record — the document that says how much money left this driver's pocket — for
an amount no human ever saw, with no second confirmation step.

THE FIX IS NOT "MAKE THE TWO EXPRESSIONS AGREE"

There is now ONE figure. `DeliveryKeyboards.reconciliation_actions` freezes the
session payload's `remaining_cash_to_submit` once and mints BOTH halves of the
button from it — the label the driver reads and the callback the tap posts. The
tap posts that frozen figure as `declared_cash`, so `submit_session` records it
verbatim instead of re-deriving one. The button and the ledger row cannot drift,
because there is only one number.

WHAT THESE TESTS MEASURE

Not return values. Each test renders the REAL screen through the REAL handler,
reads the figure off the button **the way a human reads it** (the rendered
label, with the shipped translation copy installed), taps that button, and then
asks the only question that matters: *how much cash was recorded against this
driver, and is it the number that was on the button?*

The bot's `api_client` is wired to the REAL Flask endpoints (`GET/POST
/api/v1/staff/reconciliation/session[/submit]`) with a real driver JWT, so the
whole chain is under test: keyboard → callback → handler → HTTP → view →
`DriverReconciliationService` → `DriverCashHandoff`. A canned mock of the
submit call would make every assertion here agree with itself by construction —
which is exactly how eight instances of this defect reached production through
a suite of 8,000+ tests.
"""

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.delivery import DeliveryPerson
from business_app.models.payment import CashCollectionEvent, DriverCashHandoff
from business_app.services.driver_reconciliation_service import DriverReconciliationService
from shared.enums import CashCollectionSource
from staff_bot.api_client import APIResponse
from staff_bot.handlers.delivery import status_update as status_update_module
from staff_bot.handlers.delivery.status_update import StatusUpdateHandler
from staff_bot.keyboards.delivery import DeliveryKeyboards
from tests.unit._scope_money_helpers import make_user


HANDOFF_PREFIX = DeliveryKeyboards.HANDOFF_ALL_CALLBACK


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Ctx:
    """A driver who is logged in, in English."""

    def __init__(self):
        self.user_data = {
            "language": "en",
            "authenticated": True,
            "staff_roles": ["delivery_driver"],
        }
        self.bot = MagicMock()


@pytest.fixture
def shipped_i18n(monkeypatch):
    """Install the SHIPPED staff copy on the real staff-bot i18n singleton.

    The figure a human reads only exists once the translation template has
    interpolated it. With no catalog loaded, `i18n.get` degrades to a readable
    key ("Handoff remaining cash") and every label in this file would be
    money-free — the tests would pass while measuring nothing. Using the copy
    that actually ships (rather than a convenient stand-in) also means these
    tests fail if someone edits `{amount}` out of the button.
    """
    from scripts.seed_staff_translations import STAFF_TRANSLATIONS
    from staff_bot import i18n as i18n_module

    catalog = {}
    for key, per_language in STAFF_TRANSLATIONS.items():
        for language, value in per_language.items():
            catalog.setdefault(language, {})[key] = value
    monkeypatch.setattr(i18n_module.i18n, "translations", catalog)
    return catalog


@pytest.fixture
def driver(db, delivery_driver):
    """A delivery driver an admin has not deactivated (the staff API gate)."""
    db.session.add(
        DeliveryPerson(
            user_id=delivery_driver.id,
            full_name="Test Driver",
            phone="+998900000001",
            is_active=True,
        )
    )
    db.session.commit()
    return delivery_driver


@pytest.fixture
def customer(db):
    return make_user(db)


class _StaffAPI:
    """The staff bot's `api_client`, wired to the REAL HTTP endpoints.

    Every call the handler makes goes through Flask routing, the JWT/staff-role
    gate, the view function and the service. `posted` keeps the request bodies
    so a test can assert what the *tap* actually sent, not merely what landed.
    """

    def __init__(self, client, headers):
        self._client = client
        self._headers = headers
        self.posted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    @staticmethod
    def _wrap(response) -> APIResponse:
        payload = response.get_json() or {}
        data = payload.get("data")
        return APIResponse(
            success=response.status_code < 400 and payload.get("success", True),
            data=data,
            error=payload.get("message") or payload.get("error"),
            status_code=response.status_code,
            error_code=(data or {}).get("error_code") if isinstance(data, dict) else None,
        )

    async def get_reconciliation_session(self, _token):
        return self._wrap(
            self._client.get("/api/v1/staff/reconciliation/session", headers=self._headers)
        )

    async def submit_reconciliation_session(self, _token, payload):
        self.posted.append(payload)
        return self._wrap(
            self._client.post(
                "/api/v1/staff/reconciliation/session/submit",
                json=payload,
                headers=self._headers,
            )
        )


@pytest.fixture
def bot(app, client, db, driver, monkeypatch, shipped_i18n):
    """The real handler, talking to the real API as the real driver."""
    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    api = _StaffAPI(client, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    handler = StatusUpdateHandler()
    monkeypatch.setattr(status_update_module, "api_client", api)
    monkeypatch.setattr(handler, "_get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(handler, "_get_auth_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(status_update_module.flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(status_update_module.flow_state, "clear_active", AsyncMock())
    monkeypatch.setattr(status_update_module.flow_state, "clear_and_drain", AsyncMock())
    handler._api = api
    return handler


def _update(*, data=None, text=None):
    upd = MagicMock()
    upd.effective_user = MagicMock(id=999)
    upd.message = None
    upd.callback_query = None
    if data is not None:
        upd.callback_query = MagicMock()
        upd.callback_query.data = data
        upd.callback_query.answer = AsyncMock()
        upd.callback_query.edit_message_text = AsyncMock()
    if text is not None:
        upd.message = MagicMock()
        upd.message.text = text
        upd.message.reply_text = AsyncMock()
    return upd


def _rendered(call):
    """(text, reply_markup) out of an edit_message_text / reply_text call."""
    text = call.args[0] if call.args else call.kwargs.get("text")
    return text, call.kwargs.get("reply_markup")


def open_the_screen(handler, context):
    """THE SCREEN. Draw the driver's reconciliation session, as the bot draws it."""
    upd = _update(data="staff_reconcile_session")
    asyncio.run(handler.show_reconciliation_session(upd, context))
    return _rendered(upd.callback_query.edit_message_text.call_args)


def handoff_button(markup):
    """The one button that hands cash over, or None if none is offered."""
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            if (button.callback_data or "").startswith(HANDOFF_PREFIX):
                return button
    return None


_MONEY = re.compile(r"\d[\d,   ]*(?:\.\d+)?")


def figure_a_human_reads(label: str) -> Decimal:
    """The number on the button, read the way a driver reads it.

    Deliberately parsed out of the RENDERED label rather than taken from the
    payload: if the label ever stops naming the amount that gets recorded, this
    is the assertion that notices.
    """
    matches = _MONEY.findall(label or "")
    assert matches, f"the handoff button shows no amount at all: {label!r}"
    assert len(matches) == 1, f"the handoff button shows two figures: {label!r}"
    return Decimal(re.sub(r"[,   ]", "", matches[0]))


def tap(handler, context, button):
    """THE TAP. Exactly the callback Telegram would deliver for this button."""
    upd = _update(data=button.callback_data)
    asyncio.run(handler.submit_reconciliation_all(upd, context))
    return upd


def land_cod_collection(db, session_id, customer, driver, amount, event_id):
    """A delivery completes: the engine stamps a COD collection on this session."""
    db.session.add(
        CashCollectionEvent(
            event_id=event_id,
            customer_id=customer.id,
            collector_user_id=driver.id,
            recorded_by_user_id=driver.id,
            driver_cash_session_id=session_id,
            amount=Decimal(amount),
            currency="UZS",
            source=CashCollectionSource.DELIVERY_COMPLETION,
            occurred_at=datetime.now(UTC),
        )
    )
    db.session.commit()


def open_session_id(db, driver):
    """The driver's open cash session, committed so the HTTP request sees it."""
    session = DriverReconciliationService().get_open_session_for_driver(driver.id)
    db.session.commit()
    return session.id


def handoffs(db, session_id):
    db.session.expire_all()
    return (
        DriverCashHandoff.query.filter_by(driver_cash_session_id=session_id, voided_at=None)
        .order_by(DriverCashHandoff.id)
        .all()
    )


def recorded_amounts(db, session_id):
    return [Decimal(str(handoff.amount)) for handoff in handoffs(db, session_id)]


# ---------------------------------------------------------------------------
# 🔴 THE INVARIANT — what is shown is what is recorded
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_button_records_the_amount_it_showed_when_a_cod_lands_in_the_gap(
    app, db, bot, driver, customer
):
    """🔴 THE MEASURED DEFECT. Do not delete this test.

    120,000 on the screen. A delivery completes while the driver is looking at
    it — 30,000 more cash, a real `CashCollectionEvent` against the same open
    session, exactly as `DELIVERY_COMPLETION` stamps it. Then the tap.

    The handoff that gets written must be the 120,000 the driver read. It used
    to be 150,000: the empty payload sent the server back to live data for an
    amount, and it found the driver's screen out of date.
    """
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "120000.00", "SVS-8-A")

    text, markup = open_the_screen(bot, context)
    button = handoff_button(markup)
    assert button is not None, "the driver was offered no way to hand off 120,000"
    shown = figure_a_human_reads(button.text)
    assert shown == Decimal("120000"), f"fixture drifted; the button reads {button.text!r}"
    assert "120,000" in text, f"the screen body disagrees with its own button: {text!r}"

    # THE GAP: one delivery completes. The driver's screen is already drawn.
    land_cod_collection(db, session_id, customer, driver, "30000.00", "SVS-8-B")

    tap(bot, context, button)

    written = recorded_amounts(db, session_id)
    assert written == [shown], (
        f"the button read {shown:,.0f} and {written} was written against the "
        "driver. A cash-custody record must carry the figure the human agreed "
        "to hand over, not one the server derived after they read the screen."
    )
    assert Decimal("150000") not in written, "the pre-fix figure came back"
    # And the tap is what froze it — the payload named the amount itself.
    assert Decimal(str(bot._api.posted[-1]["declared_cash"])) == shown


@pytest.mark.unit
def test_the_cash_that_landed_in_the_gap_is_offered_next_not_swallowed(
    app, db, bot, driver, customer
):
    """The remainder is deferred to a screen the driver can read — not lost.

    Recording only what was shown must not mean the 30,000 quietly disappears.
    It stays on the session and comes back as the NEXT button, named, and that
    button obeys the same rule. This is the PARTIAL shape, and it is the reason
    "just record whatever the server thinks" felt defensible in the first place.
    """
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "120000.00", "SVS-8-C")
    _, markup = open_the_screen(bot, context)
    first = handoff_button(markup)
    land_cod_collection(db, session_id, customer, driver, "30000.00", "SVS-8-D")
    tap(bot, context, first)

    text, markup = open_the_screen(bot, context)
    second = handoff_button(markup)
    assert second is not None, "the 30,000 that landed in the gap is not offered anywhere"
    still_owed = figure_a_human_reads(second.text)
    assert still_owed == Decimal("30000"), text

    tap(bot, context, second)

    assert recorded_amounts(db, session_id) == [Decimal("120000"), Decimal("30000")], (
        "both handoffs must equal the figures their own buttons displayed"
    )


@pytest.mark.unit
def test_a_quiet_session_records_exactly_what_the_button_showed(app, db, bot, driver, customer):
    """The ordinary day, end to end: nothing lands in the gap, and the whole
    expected amount is handed over and recorded once."""
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "87500.00", "SVS-8-E")

    _, markup = open_the_screen(bot, context)
    button = handoff_button(markup)
    shown = figure_a_human_reads(button.text)
    assert shown == Decimal("87500")

    tap(bot, context, button)

    assert recorded_amounts(db, session_id) == [shown]
    # Settled in full: the session closes and the driver is offered nothing more.
    _, markup = open_the_screen(bot, context)
    assert handoff_button(markup) is None, "a fully settled driver is still being asked for cash"


@pytest.mark.unit
def test_a_fractional_remainder_is_shown_in_full_and_recorded_in_full(
    app, db, bot, driver, customer
):
    """Rounding is where "the two agree" quietly stops being true.

    The label used to be `f"{amount:,.0f}"` — a 120,000.50 remainder would read
    "120,000" on the button and record 120,000.50, which is the same defect at
    a smaller scale. The frozen figure is rendered in full.
    """
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "120000.50", "SVS-8-F")

    _, markup = open_the_screen(bot, context)
    button = handoff_button(markup)
    shown = figure_a_human_reads(button.text)
    assert shown == Decimal("120000.50"), f"the button hides its own decimals: {button.text!r}"

    tap(bot, context, button)

    assert recorded_amounts(db, session_id) == [Decimal("120000.50")]


# ---------------------------------------------------------------------------
# The shapes that must stay impossible
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_amount_less_tap_writes_nothing(app, db, bot, driver, customer):
    """A tap that carries no figure must not become a cash record.

    Buttons rendered before this fix (still sitting in drivers' chat history)
    post the bare callback. There is no honest amount to write for them — the
    only way to obtain one would be to ask the server, which is the defect. The
    handler redraws the screen instead, and the redrawn button names its amount.
    """
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "120000.00", "SVS-8-G")
    open_the_screen(bot, context)
    land_cod_collection(db, session_id, customer, driver, "30000.00", "SVS-8-H")

    legacy = _update(data=HANDOFF_PREFIX)
    asyncio.run(bot.submit_reconciliation_all(legacy, context))

    assert recorded_amounts(db, session_id) == [], (
        "an amount-less tap wrote a cash-custody record anyway"
    )
    assert bot._api.posted == [], "the handler posted a handoff with no figure in it"
    text, markup = _rendered(legacy.callback_query.edit_message_text.call_args)
    redrawn = handoff_button(markup)
    assert redrawn is not None, "the driver was left with no way forward"
    assert figure_a_human_reads(redrawn.text) == Decimal("150000"), (
        "the redraw must name the current figure — 150,000 — and let the driver "
        f"agree to it before it is recorded: {text!r}"
    )


@pytest.mark.unit
def test_a_session_with_nothing_to_hand_off_offers_no_handoff_button(app, db, bot, driver):
    """No cash collected means no cash to hand over.

    The old screen still offered "Handoff all expected cash" here, and that
    button posted `{}` — so any collection landing before the tap became a
    handoff the driver had never been shown a figure for.
    """
    context = _Ctx()
    open_session_id(db, driver)

    text, markup = open_the_screen(bot, context)

    assert handoff_button(markup) is None, (
        f"an empty session is offering a cash handoff button: {text!r}"
    )


@pytest.mark.unit
def test_the_label_and_the_callback_are_one_frozen_figure(app, db, bot, driver, customer):
    """The structural half of the invariant, so a refactor cannot re-split it.

    The label is rendered from the frozen amount and the callback carries that
    same amount; reading the callback back must return exactly what the label
    said. If those two are ever minted from different expressions, this fails
    before any money moves.
    """
    context = _Ctx()
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "99999.99", "SVS-8-I")

    _, markup = open_the_screen(bot, context)
    button = handoff_button(markup)

    assert DeliveryKeyboards.parse_handoff_callback(button.callback_data) == figure_a_human_reads(
        button.text
    )


@pytest.mark.unit
def test_the_typed_amount_path_records_what_the_driver_typed(app, db, bot, driver, customer):
    """The sibling write on this screen ("Enter different amount") is one
    decision by construction — the driver's own number — and must stay that way.

    It is pinned here because it is the obvious place for someone to
    reintroduce a server-side "just settle the rest" convenience.
    """
    context = _Ctx()
    context.user_data["pending_reconciliation_flow"] = {"action": "submit"}
    session_id = open_session_id(db, driver)
    land_cod_collection(db, session_id, customer, driver, "120000.00", "SVS-8-J")
    land_cod_collection(db, session_id, customer, driver, "30000.00", "SVS-8-K")

    asyncio.run(bot.receive_reconciliation_declared_cash(_update(text="40000"), context))

    assert recorded_amounts(db, session_id) == [Decimal("40000")]
