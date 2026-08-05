"""THE DRIVER'S COD JOURNEY — screens and money, end to end, as one thing.

WHY THIS FILE EXISTS
--------------------
A 4-lens review found five instances of ONE defect: *a monetary figure shown to
a human, and the amount or scope posted to the engine, decided by different
code*. None was caught by 8 000+ tests. Three reasons were given, and all three
are structural rather than accidental:

1. **The fixtures agreed by construction.** The only fixture exercising the
   place screen gave Alice debt ONLY inside the place, where
   ``max(own, cluster, place)`` and ``union(own, coworkers)`` are the same
   number.
2. **Tests are organised by component, so nobody owned the relationship.** One
   file asserted the row was 45 000; another asserted the ceiling was a ``max``.
   Both passed. The bug lived BETWEEN them.
3. **Tests assert return values, not journeys.** Every one of the five defects
   sat exactly at the seam between a screen and an action.

So this module does not test a component. It walks the driver:

    open the debtor list  →  READ the row  →  tap the person  →  READ the
    statement  →  press Collect  →  READ the offer  →  confirm  →  and then
    asks the DATABASE where the money went.

THE CORE ASSERTION, ON EVERY JOURNEY
------------------------------------
The number rendered on the list, the number rendered on the statement, the
number offered in the collect prompt, and the number the engine actually
settles are ONE number. Every one of the four is read the way a human reads it:

* the first three off the **rendered message text / button label** — never off
  the dict behind it, because a payload key that no screen prints and a printed
  figure that no payload key holds are precisely the states the five defects
  lived in;
* the fourth off ``payments.outstanding_amount`` and
  ``cash_collection_events.unapplied_amount`` — the money itself, never a
  service's opinion of it.

HOW IT CANNOT PASS VACUOUSLY
----------------------------
* The states are GENERATED, not hand-picked: every scenario comes from
  ``tests/integration/place_state_factory``, whose oracle is arithmetic over a
  declarative spec and never calls ``business_app.services``. Each journey
  additionally checks the four rendered/settled figures against the factory's
  ``expect(...)`` oracle, so a world where production and the screens agree on
  a WRONG number still goes red.
* The screens are driven through the REAL handlers
  (``CashCollectionHandler.show_debtor_list`` → ``show_customer_statement`` →
  ``start_full_collection`` / ``start_custom_collection`` →
  ``receive_collection_amount`` → ``confirm_overpayment_collection`` →
  ``receive_collection_note``) with one shared ``context.user_data``, so the
  flow state a real conversation carries between updates is carried here too.
* The API is the REAL HTTP surface over a REAL driver JWT (:class:`_Bridge`),
  so a serializer regression, a route-level gate or a 400 shows up as a broken
  driver screen rather than as a silently green fixture.
* Copy is rendered through the REAL ``staff_bot`` i18n singleton, pointed at a
  catalog whose templates actually interpolate ``{amount}`` / ``{outstanding}``
  / ``{overpayment}`` / ``{remaining}``. The shipped seed for the receipt does
  NOT interpolate them (see ``tests/unit/test_cod_receipt_remaining_matches_offer.py``),
  which would make every "the driver reads N" assertion measure an empty string.
* Conservation is asserted on every posting journey: Σ(debt reduction) +
  Σ(prepaid-credit increase) == the amount posted. A test that watched only the
  debts would call a lost 5 000 a success.

WHAT IS PINNED AS A DEFECT
--------------------------
``_UNCOLLECTIBLE_ORDER_LINES`` (two ``TestPendingOrders`` pins). The statement's
per-order breakdown is built from every non-terminal payment and truncated to
five BEFORE the uncollectible rows are dropped, so it prints PENDING orders the
collect flow cannot settle and can push the one real debt off the screen
entirely.

WHAT WAS PINNED AND IS NOW FIXED
--------------------------------
``_GROCERY_SPLIT`` — three ``xfail(strict=True)`` pins (``TestGrocery`` ×2 and
the ``gate_on-grocery_at_place-mart`` parameter of the sweep). Every screen
offered the PLACE union while ``post_collection`` forced PERSONAL scope for a
grocery account, so the coworker's debt was never collected, the difference
became the grocery's prepaid credit, and the SAME uncollectible figure was
offered again on the next lap — indefinitely. The factory published both numbers
as data and declined to judge which was right, because judging it "belongs to a
stream that drives the screen". This was that stream; the ruling is in
``TestGrocery``'s docstring, and the display now ASKS the engine before widening
anyone (``business_app/services/cod_collect_ceiling.py::place_widening_applies``).
The three pins are gone and their tests assert the intended contract they were
always written to state.
"""

import asyncio
import re
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.payment import CashCollectionEvent, Payment
from business_app.models.user import User
from shared import business_config
from shared.enums import OrderStatus, UserRole, UserType
from staff_bot.handlers.delivery.cash_collection import CashCollectionHandler
from staff_bot.i18n import i18n as staff_i18n
from tests.integration.place_state_factory import (
    SCENARIOS,
    AddressSpec,
    DebtSpec,
    PersonSpec,
    ScenarioSpec,
    build,
    build_scenario,
)

pytestmark = pytest.mark.integration


# =========================================================================== #
# 1. THE COPY THE DRIVER ACTUALLY READS
# =========================================================================== #

#: Templates that USE every kwarg the handlers pass. Deliberately not the
#: shipped seed: ``staff.delivery.cod_collection_recorded`` ships as a bare
#: "Collection recorded successfully." with no placeholders, so ``str.format``
#: discards ``amount``/``remaining`` and a rendering assertion would pass
#: against an empty string. The contract under test is the one the HANDLER
#: declares by passing those kwargs.
#:
#: Each money-bearing line carries a UNIQUE upper-case label so a figure can be
#: located the way a human locates it — by the words next to it — instead of by
#: position.
CATALOG = {
    "staff.currency.uzs": "UZS",
    # --- debtor list ---
    "staff.delivery.cod_debtors_title": "DEBTORS",
    "staff.delivery.cod_debtors_hint": "Tap a person to open their statement",
    "staff.delivery.no_cod_debtors": "NOBODY OWES ANYTHING",
    "staff.back": "Back",
    # --- statement ---
    "staff.delivery.cod_statement_title": "STATEMENT",
    "staff.delivery.active_cod_debts": "ACTIVE DEBTS",
    "staff.delivery.collectible_now": "COLLECTIBLE NOW",
    "staff.delivery.account_cod_debts": "ON THIS ACCOUNT",
    "staff.delivery.cluster_debt_total": "LINKED ACCOUNTS OWE",
    "staff.delivery.cluster_members": "members",
    "staff.delivery.place_cod_total": "WORKPLACE OWES",
    "staff.delivery.no_cod_debt": "NO COD DEBT",
    "staff.delivery.collect_full_cod": "Collect full",
    "staff.delivery.collect_custom_cod": "Collect custom",
    "staff.order.unknown": "unknown order",
    # --- collection flow ---
    "staff.delivery.cod_collection_amount_prompt": "ENTER AMOUNT",
    "staff.delivery.cod_collection_note_prompt": "ABOUT TO COLLECT {amount}",
    "staff.delivery.cod_collection_overpayment_confirm": (
        "HANDED OVER {amount}\nOUTSTANDING NOW {outstanding}\nSURPLUS {overpayment}"
    ),
    "staff.delivery.cod_collection_recorded": (
        "COLLECTED {amount}\nSTILL COLLECTIBLE {remaining}"
    ),
    "staff.delivery.collection_notes_required": "NOTE REQUIRED",
    "staff.delivery.invalid_cash_amount": "INVALID AMOUNT",
    "staff.error_occurred": "ERROR",
    "staff.session_expired": "SESSION EXPIRED",
    "staff.unauthorized": "UNAUTHORIZED",
    "staff.yes": "Yes",
    "staff.no": "No",
    "staff.cancel": "Cancel",
}

#: ``format_currency`` renders ``f"{float(amount):,.0f} {currency}"`` and takes
#: the currency word from ``staff.currency.uzs`` — so on a rendered screen every
#: money figure, and only a money figure, looks like ``45,000 UZS``. Counts
#: ("ACTIVE DEBTS: 3") carry no currency word and are correctly not matched.
_MONEY = re.compile(r"([0-9][0-9,]*)\s+UZS")


def _figures(text):
    """Every money figure a human could read off ``text``, in reading order."""
    return [Decimal(m.replace(",", "")) for m in _MONEY.findall(text or "")]


def _figure_labelled(text, label):
    """The money figure on the rendered line bearing ``label``.

    This is the whole point of the file: the number is taken from the STRING the
    driver sees, next to the words that tell them what it means.
    """
    for line in (text or "").splitlines():
        if label in line:
            found = _figures(line)
            if found:
                return found[0]
            raise AssertionError(f"line labelled {label!r} carries no money: {line!r}")
    raise AssertionError(f"no line labelled {label!r} in:\n{text}")


def _renders(text, amount):
    """Is ``amount`` printed anywhere on this screen, as a human would see it?"""
    return Decimal(str(amount)).quantize(Decimal("1")) in [
        f.quantize(Decimal("1")) for f in _figures(text)
    ]


# =========================================================================== #
# 2. HARNESS — real HTTP, real handlers, real i18n
# =========================================================================== #


class _Resp:
    """The subset of ``staff_bot.api_client.APIResponse`` the handlers read."""

    def __init__(self, success, data=None, error=None, status_code=None, error_code=None):
        self.success = success
        self.data = data
        self.error = error
        self.status_code = status_code
        self.error_code = error_code


class _Bridge:
    """Async-context stand-in for ``staff_bot.api_client`` speaking real HTTP.

    Mirrors ``StaffAPIClient._make_request``'s contract: 200/201 unwrap
    ``payload['data']``, anything else is a failure carrying the backend's
    ``error_code``. Every call is recorded so a test can assert the exact POST
    body the driver's tap produced — the payload is the other half of the seam
    (a screen may show the right number and post the wrong SCOPE).
    """

    def __init__(self, http, token):
        self.http = http
        self.token = token
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _send(self, method, path, payload=None, params=None):
        self.calls.append({"method": method, "path": path, "payload": payload})
        response = self.http.open(
            path,
            method=method,
            json=payload,
            query_string=params,
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
        return [c for c in self.calls if c["path"].endswith(suffix) and c["method"] == "POST"]

    # -- the three endpoints the COD flow uses -----------------------------

    async def get_cod_debtors(self, token, *, page=1, per_page=10):
        return self._send(
            "GET",
            "/api/v1/staff/customers/with-open-cod",
            params={"page": page, "per_page": per_page},
        )

    async def get_customer_cod_statement(self, token, customer_id):
        return self._send("GET", f"/api/v1/staff/customers/{customer_id}/cod-statement")

    async def record_cash_collection(self, token, payload):
        return self._send("POST", "/api/v1/staff/cash-collections", payload)


@pytest.fixture
def http(app):
    """A FRESH test client per test — the session-scoped ``client`` leaks JWT
    cookies between tests."""
    return app.test_client()


@pytest.fixture
def driver(db):
    """The human holding the phone. Real staff user; the route makes them the
    collector via ``get_jwt_identity()``."""
    user = User(
        email=f"journey-driver-{uuid4().hex[:8]}@example.com",
        phone=f"+99894{uuid4().int % 10000000:07d}",
        password_hash="x",
        first_name="Journey",
        last_name="Driver",
        user_type=UserType.STAFF,
        role=UserRole.DELIVERY_DRIVER,
        is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def bridge(app, http, driver):
    with app.app_context():
        token = create_access_token(identity=str(driver.id))
    return _Bridge(http, token)


@pytest.fixture(autouse=True)
def _rendered_copy(monkeypatch):
    """Point the REAL staff_bot i18n singleton at :data:`CATALOG`.

    ``staff_bot.utils.formatters`` and the handler module bind the same
    singleton, so one patch covers ``format_currency`` and every template.
    Without it, ``i18n.get`` humanises the missing key's last segment and DROPS
    every interpolation kwarg — the money would vanish from the screens and
    every assertion below would measure nothing.
    """
    catalog = {lang: dict(CATALOG) for lang in ("en", "uz", "ru")}
    monkeypatch.setattr(staff_i18n, "translations", catalog)


@pytest.fixture(autouse=True)
def _no_redis_flow_state(monkeypatch):
    """``flow_state`` mirrors the flow flag into Redis; irrelevant to money."""
    from staff_bot.utils import flow_state

    monkeypatch.setattr(flow_state, "mark_active", AsyncMock())
    monkeypatch.setattr(flow_state, "clear_and_drain", AsyncMock())


@pytest.fixture
def gate(app, monkeypatch):
    """Both halves of ``PLACE_COD_COLLECTION_ENABLED``, set together.

    The bot reads ``shared.business_config``; the route reads
    ``current_app.config``. Setting one without the other produces a run
    LABELLED "gate off" that is half on — which is a way to test nothing at all.
    ``app`` is session-scoped, so the Flask mirror is restored on teardown.
    """
    original = app.config.get("PLACE_COD_COLLECTION_ENABLED")

    def _set(enabled):
        app.config["PLACE_COD_COLLECTION_ENABLED"] = enabled
        monkeypatch.setattr(business_config, "PLACE_COD_COLLECTION_ENABLED", enabled)

    _set(True)
    yield _set
    app.config["PLACE_COD_COLLECTION_ENABLED"] = original


# =========================================================================== #
# 3. THE JOURNEY — one object per driver, one ``user_data`` across all steps
# =========================================================================== #


def _callback_update(data):
    update = MagicMock()
    update.effective_user = MagicMock(id=4242)
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.message = None
    return update


def _message_update(text):
    update = MagicMock()
    update.effective_user = MagicMock(id=4242)
    update.callback_query = None
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _edited(update):
    call = update.callback_query.edit_message_text.call_args
    assert call is not None, "handler edited no message"
    return call.args[0] if call.args else call.kwargs["text"]


def _replied(update):
    call = update.message.reply_text.call_args
    assert call is not None, "handler sent no message"
    return call.args[0] if call.args else call.kwargs["text"]


def _markup(update):
    return update.callback_query.edit_message_text.call_args.kwargs.get("reply_markup")


class Journey:
    """A driver walking the COD screens. One conversation, many updates.

    Every step returns the RENDERED text of the screen that step produced. The
    tests read money out of those strings; nothing here reaches into the payload
    behind them.
    """

    def __init__(self, bridge, monkeypatch):
        from staff_bot.handlers.delivery import cash_collection as module

        self.handler = CashCollectionHandler()
        self.bridge = bridge
        monkeypatch.setattr(module, "api_client", bridge)
        monkeypatch.setattr(self.handler, "_get_language", AsyncMock(return_value="en"))
        monkeypatch.setattr(self.handler, "_get_auth_token", AsyncMock(return_value="tok"))
        self.context = MagicMock()
        # ONE user_data for the whole journey — the flow dict the real
        # conversation carries between updates lives here.
        self.context.user_data = {
            "language": "en",
            "authenticated": True,
            "staff_roles": ["delivery_driver"],
        }
        self.context.bot = MagicMock()
        self.last_markup = None
        self.alerts = []

    # -- screens ----------------------------------------------------------

    def open_debtor_list(self, page=1):
        update = _callback_update("staff_cod_collect_menu")
        asyncio.run(self.handler.show_debtor_list(update, self.context, page=page))
        self.last_markup = _markup(update)
        return _edited(update)

    def row_labels(self):
        """Every button label on the currently rendered list, as printed."""
        assert self.last_markup is not None, "open the list first"
        return [b.text for row in self.last_markup.inline_keyboard for b in row]

    def row_for(self, name):
        """The list row a driver looking for ``name`` would tap: (label, id)."""
        for row in self.last_markup.inline_keyboard:
            for button in row:
                if name in button.text and button.callback_data.startswith(
                    "staff_cod_customer_"
                ):
                    return button.text, int(button.callback_data.rsplit("_", 1)[-1])
        raise AssertionError(f"no debtor row for {name!r} in {self.row_labels()}")

    def tap_person(self, customer_id):
        update = _callback_update(f"staff_cod_customer_{customer_id}")
        asyncio.run(self.handler.show_customer_statement(update, self.context))
        self.last_markup = _markup(update)
        return _edited(update)

    def statement_buttons(self):
        return [b.callback_data for row in self.last_markup.inline_keyboard for b in row]

    def press_collect_full(self, customer_id):
        update = _callback_update(f"staff_cod_collect_full_{customer_id}")
        asyncio.run(self.handler.start_full_collection(update, self.context))
        self.alerts = [
            c for c in update.callback_query.answer.call_args_list if c.args or c.kwargs
        ]
        if update.callback_query.edit_message_text.call_args is None:
            return None  # refused (alert only)
        return _edited(update)

    def press_collect_custom(self, customer_id):
        update = _callback_update(f"staff_cod_collect_custom_{customer_id}")
        asyncio.run(self.handler.start_custom_collection(update, self.context))
        return _edited(update)

    def type_amount(self, text):
        update = _message_update(text)
        asyncio.run(self.handler.receive_collection_amount(update, self.context))
        return _replied(update)

    def confirm_surplus(self):
        update = _callback_update("staff_cod_confirm_overpay_yes")
        asyncio.run(self.handler.confirm_overpayment_collection(update, self.context))
        return _edited(update)

    def decline_surplus(self):
        update = _callback_update("staff_cod_confirm_overpay_no")
        asyncio.run(self.handler.cancel_overpayment_collection(update, self.context))
        return _edited(update)

    def type_note(self, text="collected at the door"):
        update = _message_update(text)
        asyncio.run(self.handler.receive_collection_note(update, self.context))
        return _replied(update)

    # -- what was posted --------------------------------------------------

    def posted_collection(self):
        posts = self.bridge.posted("/staff/cash-collections")
        assert posts, "no collection was posted"
        return posts[-1]["payload"]


@pytest.fixture
def journey(bridge, monkeypatch):
    return Journey(bridge, monkeypatch)


# =========================================================================== #
# 4. THE MONEY — measured off the rows, never off a service
# =========================================================================== #


def _outstanding(db, scenario):
    """``{debt_key: outstanding}`` straight off ``payments``.

    ``expire_all`` first: the collection was committed by a request running on
    this same scoped session, so every row the test already touched is sitting
    in the identity map at its PRE-collection value. Reading it without
    expiring would report "nothing moved" for a collection that moved
    everything — a measurement that fails safe only by accident.
    """
    db.session.expire_all()
    return {
        key: Decimal(str(db.session.get(Payment, payment.id).outstanding_amount)).quantize(
            Decimal("0.01")
        )
        for key, payment in scenario.payments.items()
    }


def _credit(db, user_ids):
    """Unapplied prepaid credit held by these accounts — the surplus wallet."""
    db.session.expire_all()
    total = Decimal("0.00")
    for event in (
        db.session.query(CashCollectionEvent)
        .filter(CashCollectionEvent.customer_id.in_(list(user_ids)))
        .all()
    ):
        total += Decimal(str(event.unapplied_amount or 0))
    return total.quantize(Decimal("0.01"))


class Money:
    """Before/after snapshot of every debt and every wallet in the scenario."""

    def __init__(self, db, scenario):
        self.db = db
        self.scenario = scenario
        self.person_keys = [p.key for p in scenario.spec.people]
        self.debts_before = _outstanding(db, scenario)
        self.credit_before = {
            person: _credit(db, scenario.expect(person).cluster_user_ids)
            for person in self.person_keys
        }

    def settled(self):
        """``{debt_key: amount that debt went DOWN by}`` — only movers."""
        after = _outstanding(self.db, self.scenario)
        return {
            key: self.debts_before[key] - value
            for key, value in after.items()
            if self.debts_before[key] != value
        }

    def total_settled(self):
        after = _outstanding(self.db, self.scenario)
        return sum(
            (self.debts_before[k] - v for k, v in after.items()), Decimal("0.00")
        ).quantize(Decimal("0.01"))

    def credit_delta(self):
        """``{person_key: increase in that person's cluster prepaid credit}``."""
        out = {}
        for person, before in self.credit_before.items():
            after = _credit(self.db, self.scenario.expect(person).cluster_user_ids)
            if after != before:
                out[person] = after - before
        return out

    def total_credit_delta(self):
        return sum(self.credit_delta().values(), Decimal("0.00")).quantize(Decimal("0.01"))


def money(db, scenario):
    """Snapshot the world's money NOW; every ``settled()`` call diffs against it."""
    return Money(db, scenario)


def _assert_conservation(snapshot, posted_amount):
    """Every som handed over either killed debt or became credit.

    A journey that watched only the debts would score a silently lost 5 000 as a
    success.
    """
    posted = Decimal(str(posted_amount)).quantize(Decimal("0.01"))
    assert snapshot.total_settled() + snapshot.total_credit_delta() == posted, (
        f"cash posted {posted} but debts fell by {snapshot.total_settled()} and "
        f"credit rose by {snapshot.total_credit_delta()}"
    )


# =========================================================================== #
# 5. THE DEFECTS THIS FILE FOUND — one reason string, shared by every pin
# =========================================================================== #

#: 🔴 MEASURED, not inferred. See the two ``TestPendingOrders`` pins and
#: ``TestGrocery`` below; each states the numbers it actually observed.
_UNCOLLECTIBLE_ORDER_LINES = (
    "BUG: the statement's per-order lines include orders the collect flow "
    "cannot settle, and are truncated before they are filtered. "
    "`get_customer_cod_statement` appends an `items` row for EVERY payment — "
    "the `items.append` call sits outside the terminal-status filter, so "
    "PENDING orders are in — and `_format_statement` renders `items[:5]`, "
    "slicing FIRST and skipping zero-outstanding rows second. Two measured "
    "consequences: (1) on the canonical A6 rows plus one PENDING 70 000 the "
    "driver reads a '70,000' order line under a '45,000 COLLECTIBLE NOW' "
    "headline and the printed lines sum to 95 000 — the per-account figure the "
    "headline was fixed to stop showing; (2) with six newer PENDING orders "
    "ahead of it, a real 9 000 delivered debt is pushed off the screen "
    "entirely: the headline says 9,000 and the five visible lines are five "
    "uncollectible 1,000s. `_format_statement`'s own docstring states the "
    "invariant this breaks — 'EVERY MONEY FIGURE HERE IS EITHER THE OFFER OR A "
    "LABELLED COMPONENT OF IT'. A PENDING order is neither."
)

# NOTE: ``_GROCERY_SPLIT`` used to live here — the reason string on three
# ``xfail(strict=True)`` pins. The defect it named is fixed, the pins are gone,
# and the history is kept in ``TestGrocery``'s docstring rather than in a
# constant nothing references.


# =========================================================================== #
# 6. THE GUARD ON THE GUARD — every "N is not on this screen" assertion in this
#    file is worthless if the screens render no money at all.
# =========================================================================== #


class TestTheHarnessCanSeeMoney:
    """If ``i18n`` is not patched, ``i18n.get`` humanises the missing key and
    silently DROPS every interpolation kwarg, ``format_currency`` loses its
    currency word, and ``_figures`` returns ``[]`` for everything. Every
    negative assertion in this module ("95 000 appears on no screen") would then
    pass while measuring nothing. These four tests fail first if that happens.
    """

    def test_the_reader_parses_money_and_only_money(self):
        assert _figures("💰 <b>COLLECTIBLE NOW: 45,000 UZS</b>") == [Decimal("45000")]
        assert _figures("💳 ACTIVE DEBTS: 3") == []  # a count is not money
        assert _figure_labelled("A: 1,000 UZS\nB: 2,000 UZS", "B") == Decimal("2000")

    def test_the_reader_refuses_to_read_a_screen_that_lost_its_figure(self):
        with pytest.raises(AssertionError):
            _figure_labelled("COLLECTIBLE NOW: —", "COLLECTIBLE NOW")
        with pytest.raises(AssertionError):
            _figure_labelled("nothing here", "COLLECTIBLE NOW")

    def test_the_real_screens_render_real_money(self, db, journey, gate):
        """Not a parse of a literal — the actual rendered statement and offer."""
        s = build(db, "a6_canonical")
        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)

        assert "45,000 UZS" in label
        assert "45,000 UZS" in statement
        assert "45,000 UZS" in prompt
        assert s.expect("alice").collectible_total == Decimal("45000.00")

    def test_the_divergent_state_really_is_divergent(self, db, gate):
        """The whole effort rests on ``a6_canonical`` being a world where the
        shipped ``max(own, cluster, place)`` and the correct union differ. If a
        factory change ever collapsed them, every journey below would still pass
        while testing the fixture that agreed by construction."""
        s = build(db, "a6_canonical")
        alice = s.expect("alice")
        shipped_max = max(
            alice.account_delivered_outstanding,
            alice.cluster_delivered_outstanding,
            s.place_expect("g").open_cod_total,
        )
        assert shipped_max == Decimal("35000.00")
        assert alice.collectible_total == Decimal("45000.00")
        assert shipped_max != alice.collectible_total


# =========================================================================== #
# 7. THE JOURNEYS
# =========================================================================== #


class TestOverPayment:
    """More cash than the debt: the surplus must land on the RIGHT wallet."""

    def test_surplus_goes_to_the_payer_not_the_coworker(self, db, journey, gate):
        """A6 canonical — debt BOTH inside and outside the place, the state in
        which ``max(own, cluster, place)`` and ``union`` finally differ.

        Alice: 10 000 at her ungrouped home + 15 000 at office G.
        Bob:   20 000 at office G.
        Her collectible union is 45 000; the ``max`` that shipped was 35 000.

        The driver is handed 50 000. The screens must all say 45 000, the engine
        must settle exactly those three debts, and the 5 000 surplus must become
        ALICE's prepaid credit — not Bob's, and not a fourth debt's payment.
        """
        s = build(db, "a6_canonical")
        alice, bob = s.expect("alice"), s.expect("bob")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        row_figure = _figures(label)[0]
        assert customer_id in alice.cluster_user_ids

        statement = journey.tap_person(customer_id)
        screen_figure = _figure_labelled(statement, "COLLECTIBLE NOW")

        journey.press_collect_custom(customer_id)
        confirm = journey.type_amount("50000")
        offered = _figure_labelled(confirm, "OUTSTANDING NOW")
        surplus_shown = _figure_labelled(confirm, "SURPLUS")

        journey.confirm_surplus()
        journey.type_note("50 000 handed over at the office")

        # --- one number, four independent readings ---------------------
        assert row_figure == screen_figure == offered == alice.collectible_total
        assert alice.collectible_total == Decimal("45000.00")  # the oracle, spelled out
        assert surplus_shown == Decimal("5000.00")

        # --- and the money agrees with all four ------------------------
        assert snapshot.total_settled() == offered
        assert snapshot.settled() == {
            "alice_home": Decimal("10000.00"),
            "alice_office": Decimal("15000.00"),
            "bob_office": Decimal("20000.00"),
        }
        assert snapshot.credit_delta() == {"alice": Decimal("5000.00")}
        assert bob.user_id not in alice.cluster_user_ids  # the surplus is not Bob's
        _assert_conservation(snapshot, 50000)

    def test_declining_the_surplus_re_prompts_and_moves_nothing(self, db, journey, gate):
        """"No" on the surplus screen must leave the world untouched — a driver
        backing out of an over-payment is not a collection."""
        s = build(db, "a6_canonical")
        snapshot = money(db, s)

        journey.open_debtor_list()
        _, customer_id = journey.row_for("Alice")
        journey.tap_person(customer_id)
        journey.press_collect_custom(customer_id)
        journey.type_amount("50000")
        re_prompt = journey.decline_surplus()

        assert "ENTER AMOUNT" in re_prompt
        assert snapshot.settled() == {}
        assert snapshot.credit_delta() == {}
        assert not journey.bridge.posted("/staff/cash-collections")


class TestExactPayment:
    def test_exact_collection_clears_the_office_and_creates_no_credit(
        self, db, journey, gate
    ):
        """``debt_inside_place_only`` — THE FIXTURE THAT AGREED BY CONSTRUCTION,
        kept deliberately. Here union == place total == max == 35 000, so this
        journey passing proves only that the happy path works; it is the same
        journey on ``a6_canonical`` (above) that has teeth. Both are run so the
        pair can disagree.
        """
        s = build(db, "debt_inside_place_only")
        alice = s.expect("alice")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        receipt = journey.type_note("exact money at the door")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == alice.collectible_total
            == Decimal("35000.00")
        )
        assert snapshot.total_settled() == offered
        assert snapshot.settled() == {
            "alice_office": Decimal("15000.00"),
            "bob_office": Decimal("20000.00"),
        }
        assert snapshot.credit_delta() == {}
        assert _figure_labelled(receipt, "STILL COLLECTIBLE") == Decimal("0")
        _assert_conservation(snapshot, 35000)


class TestPartialPayment:
    def test_partial_collection_leaves_the_rest_collectible(self, db, journey, gate):
        """``solo_ungrouped_debtor`` — the pre-place baseline, and the only shape
        with exactly ONE open debt, so the allocation target is unambiguous.

        15 000 DELIVERED + 5 000 PENDING + 9 000 already SETTLED, plus 1 200 of
        pre-existing prepaid credit. The driver takes 5 000. The PENDING order
        must not move (the engine's rings are DELIVERED-only), the settled one
        must not move, the old credit must not be consumed, and the next offer
        must be exactly 10 000.
        """
        s = build(db, "solo_ungrouped_debtor")
        sam = s.expect("sam")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Sam")
        statement = journey.tap_person(customer_id)
        journey.press_collect_custom(customer_id)
        note_prompt = journey.type_amount("5000")
        receipt = journey.type_note("part payment")

        shown = _figure_labelled(statement, "COLLECTIBLE NOW")
        assert _figures(label)[0] == shown == sam.collectible_total == Decimal("15000.00")
        # The 20 000 per-account headline (PENDING included) is what the fifth
        # instance of the defect rendered. It must be on no screen at all.
        assert sam.account_outstanding_amount == Decimal("20000.00")
        assert not _renders(statement, 20000)

        assert _figure_labelled(note_prompt, "ABOUT TO COLLECT") == Decimal("5000")
        assert snapshot.settled() == {"sam_delivered": Decimal("5000.00")}
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 5000)

        # The receipt states what is STILL collectible, and the next offer must
        # honour it — the sixth instance of the same split lived exactly here.
        remaining = _figure_labelled(receipt, "STILL COLLECTIBLE")
        assert remaining == Decimal("10000")
        next_statement = journey.tap_person(customer_id)
        assert _figure_labelled(next_statement, "COLLECTIBLE NOW") == remaining
        assert (
            _figure_labelled(journey.press_collect_full(customer_id), "ABOUT TO COLLECT")
            == remaining
        )


class TestDebtFreeCoworker:
    def test_a_coworker_who_owes_nothing_can_still_clear_the_office(
        self, db, journey, gate
    ):
        """``debt_free_coworker`` — the shape rule 3 exists for.

        Bob owns an address at office G, has NO orders at all, and must still
        appear on the debtor list at 15 000 (the synthesised row) because the
        office's debt is collectible THROUGH a person (owner ruling A7). The
        money must land on ALICE's order, and Bob must end with no credit and no
        debt of his own.
        """
        s = build(db, "debt_free_coworker")
        bob = s.expect("bob")
        assert bob.expected_row_is_synthesised  # the state under test exists
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Bob")
        assert customer_id == bob.user_id

        statement = journey.tap_person(customer_id)
        assert "staff_cod_collect_full_%d" % customer_id in journey.statement_buttons()
        prompt = journey.press_collect_full(customer_id)
        receipt = journey.type_note("Bob paid for the office")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == bob.collectible_total
            == Decimal("15000.00")
        )
        assert snapshot.total_settled() == offered
        assert snapshot.settled() == {"alice_office": Decimal("15000.00")}
        assert snapshot.credit_delta() == {}
        assert _figure_labelled(receipt, "STILL COLLECTIBLE") == Decimal("0")
        _assert_conservation(snapshot, 15000)


class TestLinkedSiblings:
    def test_one_person_two_phone_accounts_is_one_row_and_one_settlement(
        self, db, journey, gate
    ):
        """``cod_exempt_cluster`` — one person, two accounts, 30 000 + 40 000.

        The list carries ONE row for the person (keyed on the account with the
        larger own debt), the statement states the linked total, and a single
        collection settles BOTH accounts' debts. Pre-existing credit is pooled
        across the cluster (1 000 + 500) and must not be disturbed.
        """
        s = build(db, "cod_exempt_cluster")
        vip = s.expect("vip_a")
        assert vip.prepaid_credit == Decimal("1500.00")
        snapshot = money(db, s)

        journey.open_debtor_list()
        rows = [r for r in journey.row_labels() if "Vip" in r]
        assert len(rows) == 1, f"one person must be one row, got {rows}"
        label, customer_id = journey.row_for("Vip")
        assert customer_id in vip.cluster_user_ids

        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        journey.type_note("both numbers settled together")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == vip.collectible_total
            == Decimal("70000.00")
        )
        assert snapshot.settled() == {
            "vip_a_debt": Decimal("30000.00"),
            "vip_b_debt": Decimal("40000.00"),
        }
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 70000)

    def test_the_office_is_reachable_through_the_sibling_who_owns_no_debt(
        self, db, journey, gate
    ):
        """``sibling_owns_place_address`` — THE RULE-3 GAP.

        One person with two accounts: ``alice_a`` owes 10 000 at her ungrouped
        home; ``alice_b`` owns the office address in G and owes nothing; Bob owes
        20 000 at G. A composition that discovers places through the accounts
        that CARRY debt never finds G for this person and loses Bob's 20 000.
        The cluster is worth 30 000, on every screen, and the collection must
        reach Bob's order through an address the payer's SIBLING owns.
        """
        s = build(db, "sibling_owns_place_address")
        alice = s.expect("alice_a")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice A")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        journey.type_note("paid for herself and the office")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == alice.collectible_total
            == Decimal("30000.00")
        )
        # The scope posted is the sibling's office address — the only address
        # that can widen this collection to the coworker's debt.
        assert journey.posted_collection()["delivery_address_id"] == s.address("alice_office").id
        assert snapshot.settled() == {
            "alice_home": Decimal("10000.00"),
            "bob_office": Decimal("20000.00"),
        }
        # The sibling's pre-existing 2 500 credit is untouched by a collection
        # that settled exactly its ceiling.
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 30000)


class TestPendingOrders:
    def test_a_pending_order_never_reaches_a_screen_or_the_engine(
        self, db, journey, gate
    ):
        """``a6_with_pending`` — the measured admin-modal defect, on the driver's
        screens.

        A 70 000 PENDING order inflates Alice's per-account headline to 95 000
        while a collection can still settle only 45 000. 95 000 must appear on
        NO screen, the PENDING order's payment must not move, and the receipt —
        which used to read the per-account headline — must state 0 remaining.
        """
        s = build(db, "a6_with_pending")
        alice = s.expect("alice")
        assert alice.account_outstanding_amount == Decimal("95000.00")
        snapshot = money(db, s)

        list_screen = journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        receipt = journey.type_note("full collection at the office")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == alice.collectible_total
            == Decimal("45000.00")
        )
        for screen, name in (
            (list_screen + "\n" + "\n".join(journey.row_labels()), "list"),
            (statement, "statement"),
            (prompt, "collect prompt"),
            (receipt, "receipt"),
        ):
            assert not _renders(screen, 95000), f"PENDING-inflated headline on the {name}"

        assert snapshot.settled() == {
            "alice_home": Decimal("10000.00"),
            "alice_office": Decimal("15000.00"),
            "bob_office": Decimal("20000.00"),
        }
        assert _figure_labelled(receipt, "STILL COLLECTIBLE") == Decimal("0")
        _assert_conservation(snapshot, 45000)

    @pytest.mark.xfail(strict=True, reason=_UNCOLLECTIBLE_ORDER_LINES)
    def test_the_statement_lists_only_orders_the_collect_flow_can_settle(
        self, db, journey, gate
    ):
        """The per-order breakdown is a screen figure like any other.

        The four headline figures agree (pinned by the test above); this is the
        SAME invariant one line lower down the same screen, where it does not
        hold. The contract asserted here is the intended one: every order line
        the driver reads before pressing Collect names debt that pressing
        Collect will actually pay.

        MEASURED on these rows: the driver reads a 70 000 order line under a
        45 000 "COLLECTIBLE NOW" headline, and the three printed lines sum to
        95 000 — the exact per-account figure the headline was fixed to stop
        showing.
        """
        s = build(db, "a6_with_pending")
        pending_order = s.order("alice_pending")

        journey.open_debtor_list()
        _, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)

        assert pending_order.order_number not in statement
        assert not _renders(statement, 70000)

    @pytest.mark.xfail(strict=True, reason=_UNCOLLECTIBLE_ORDER_LINES)
    def test_pending_orders_cannot_push_a_real_debt_off_the_statement(
        self, db, journey, gate
    ):
        """The harm the truncation does, on a state built for the purpose.

        ``_format_statement`` slices ``items[:5]`` and only THEN skips
        zero-outstanding rows — the uncollectible PENDING lines are never
        dropped, so they consume the budget. This ad-hoc spec (the factory takes
        one inline) gives Pat ONE real 9 000 delivered debt and six NEWER
        pending orders.

        MEASURED: the statement's headline reads "COLLECTIBLE NOW 9,000" and
        then lists five 1,000 PENDING lines. The order the 9 000 actually lives
        on is not on the screen at all. Every printed figure is uncollectible
        and the one collectible debt is invisible — a driver reconciling cash
        against the breakdown has nothing true to reconcile against.
        """
        spec = ScenarioSpec(
            name="journey_pending_flood",
            doc="one DELIVERED debt buried under six newer PENDING orders",
            people=(PersonSpec("pat"),),
            addresses=(AddressSpec("pat_home", owner="pat", title="home"),),
            debts=(DebtSpec("pat_real", owner="pat", at="pat_home", amount="9000"),)
            + tuple(
                DebtSpec(
                    f"pat_pending_{i}",
                    owner="pat",
                    at="pat_home",
                    amount="1000",
                    status=OrderStatus.PENDING,
                )
                for i in range(6)
            ),
        )
        s = build_scenario(db, spec)
        real_order = s.order("pat_real")

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Pat")
        statement = journey.tap_person(customer_id)

        assert _figures(label)[0] == Decimal("9000")
        assert _figure_labelled(statement, "COLLECTIBLE NOW") == Decimal("9000")
        # The debt the headline names must be visible in the breakdown.
        assert real_order.order_number in statement
        # And nothing uncollectible may occupy the five visible lines.
        assert not _renders(statement, 1000)


class TestGateOff:
    def test_gate_off_shows_and_settles_the_un_widened_figure(self, db, journey, gate):
        """``PLACE_COD_COLLECTION_ENABLED=false`` is the rollback switch, and a
        rollback that changes the payload but not the money is not a rollback.

        On the SAME A6 rows the whole journey must degrade together: row,
        statement, offer and settlement all become Alice's own cluster debt
        (25 000), the post must carry NO ``delivery_address_id``, and Bob's
        20 000 must be untouched — Plan D's behaviour, exactly.
        """
        gate(False)
        s = build(db, "a6_canonical")
        alice = s.expect("alice")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        journey.type_note("gate off collection")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == alice.cluster_delivered_outstanding
            == Decimal("25000.00")
        )
        assert not _renders(statement, 45000)
        assert journey.posted_collection()["delivery_address_id"] is None
        assert snapshot.settled() == {
            "alice_home": Decimal("10000.00"),
            "alice_office": Decimal("15000.00"),
        }
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 25000)


class TestAmbiguousPlace:
    def test_two_places_degrade_the_figure_and_the_scope_together(
        self, db, journey, gate
    ):
        """Decision E7 — ``two_places_one_cluster``.

        Alice owns an address in G1 AND in G2, so no screen can name which place
        a collection is for. The figure and the address must degrade TOGETHER to
        the un-widened cluster figure (10 000, no address). The union across both
        places is 35 000; the shipped P0-degraded defect kept the PLACE scope
        while falling back on the number, so a 25 000 ceiling sat over a 45 000
        settlement. Here: 35 000 must appear nowhere, and neither coworker's debt
        may move.
        """
        s = build(db, "two_places_one_cluster")
        alice = s.expect("alice")
        assert alice.collect_scope_type == "cluster"
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        journey.type_note("ambiguous place")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == alice.collect_scope_amount
            == Decimal("10000.00")
        )
        assert not _renders(statement, 35000)
        assert journey.posted_collection()["delivery_address_id"] is None
        assert snapshot.settled() == {"alice_g1": Decimal("10000.00")}
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 10000)


class TestThreeMembers:
    def test_three_coworkers_read_three_different_numbers_off_one_place(
        self, db, journey, gate
    ):
        """``three_member_place`` — 15 000 / 12 000 / 12 000 over a 12 000 place.

        A surface that renders the PLACE total cannot produce this set, so this
        is the shape that catches a screen quietly substituting the place's own
        debt for the person's collectible. Each row is then walked to its offer.
        """
        s = build(db, "three_member_place")
        expected = {
            "Ann": s.expect("ann").collectible_total,
            "Ben": s.expect("ben").collectible_total,
            "Cara": s.expect("cara").collectible_total,
        }
        assert expected == {
            "Ann": Decimal("15000.00"),
            "Ben": Decimal("12000.00"),
            "Cara": Decimal("12000.00"),
        }
        assert s.place_expect("g").open_cod_total == Decimal("12000.00")

        journey.open_debtor_list()
        for name, oracle in expected.items():
            label, customer_id = journey.row_for(name)
            statement = journey.tap_person(customer_id)
            prompt = journey.press_collect_full(customer_id)
            assert (
                _figures(label)[0]
                == _figure_labelled(statement, "COLLECTIBLE NOW")
                == _figure_labelled(prompt, "ABOUT TO COLLECT")
                == oracle
            ), f"{name}'s journey disagrees with the oracle"
            journey.open_debtor_list()  # back out without collecting

    def test_collecting_from_the_debt_free_member_clears_both_coworkers(
        self, db, journey, gate
    ):
        """Ben owes nothing and is worth 12 000 — Ann's office 5 000 plus Cara's
        7 000, and NOT Ann's 3 000 home debt, which is outside the place."""
        s = build(db, "three_member_place")
        snapshot = money(db, s)

        journey.open_debtor_list()
        _, ben_id = journey.row_for("Ben")
        journey.tap_person(ben_id)
        prompt = journey.press_collect_full(ben_id)
        journey.type_note("Ben settled the office")

        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        assert offered == Decimal("12000")
        assert snapshot.settled() == {
            "ann_office": Decimal("5000.00"),
            "cara_office": Decimal("7000.00"),
        }
        assert "ann_home" not in snapshot.settled()
        assert snapshot.credit_delta() == {}
        _assert_conservation(snapshot, 12000)


class TestNothingToCollect:
    def test_the_empty_world_offers_nothing_and_moves_nothing(self, db, journey, gate):
        """``zero_everything`` — the control that catches an assertion which
        passes because it is measuring nothing."""
        s = build(db, "zero_everything")
        nora = s.expect("nora")
        assert nora.collectible_total == Decimal("0.00")
        snapshot = money(db, s)

        list_screen = journey.open_debtor_list()
        assert "NOBODY OWES ANYTHING" in list_screen
        assert not [r for r in journey.row_labels() if "Nora" in r]

        # The person still has a statement if reached directly; it must offer
        # nothing and refuse the collect flow.
        statement = journey.tap_person(nora.user_id)
        assert _figure_labelled(statement, "COLLECTIBLE NOW") == Decimal("0")
        assert not [
            b for b in journey.statement_buttons() if b.startswith("staff_cod_collect_")
        ]
        assert journey.press_collect_full(nora.user_id) is None
        assert snapshot.settled() == {}
        assert snapshot.credit_delta() == {}
        assert not journey.bridge.posted("/staff/cash-collections")


# =========================================================================== #
# 8. THE NEGATIVE — no screen may show a figure the next action will not honour
# =========================================================================== #

#: 🔴 GENERATED, NOT HAND-PICKED — every preset in the factory, crossed with
#: every person in it. This is the direct answer to "the fixtures agreed by
#: construction": nobody chooses which shapes get walked, so a new scenario
#: added to the factory is walked the moment it lands and a divergent state
#: cannot stay invisible for want of somebody remembering to write its test.
#:
#: ONE FRESH WORLD PER PERSON. A collection settles debt, so walking two people
#: in one database would test the second against a world the first already
#: emptied — and would report that as a production refusal. Each parameter
#: therefore builds its own scenario and collects exactly once.
def _walk_parameters():
    params = []
    for gate_on in (True, False):
        state = "gate_on" if gate_on else "gate_off"
        for name in sorted(SCENARIOS):
            for person in SCENARIOS[name].people:
                # 🔴 NO MARKS, AND NO WAY TO ADD ONE. This loop used to carry an
                # `xfail(strict=True)` for `gate_on-grocery_at_place-mart` — the
                # one generated case where the shown figure and the settled set
                # parted company. It is fixed, so the sweep is now uniform: every
                # preset × every person × both gate states, held to the same
                # contract with no exception anywhere. An exception here IS the
                # bug this file exists to catch.
                params.append(
                    pytest.param(
                        name,
                        person.key,
                        gate_on,
                        id=f"{state}-{name}-{person.key}",
                    )
                )
    return params


@pytest.mark.parametrize("scenario_name,person_key,gate_on", _walk_parameters())
def test_no_screen_shows_a_figure_the_next_action_will_not_honour(
    db, journey, gate, scenario_name, person_key, gate_on
):
    """THE INVARIANT, over every generated shape, in BOTH gate states.

    For one person in one freshly built world: the figure printed on their
    debtor-list row, the figure printed on their statement, and the figure
    printed in the collect prompt are ONE number — and posting exactly that
    number reduces debt by exactly that number, with nothing silently becoming
    prepaid credit.

    Every figure is read off RENDERED TEXT and then checked against the
    factory's oracle, which is arithmetic over the declarative spec and never
    calls the code under test. So three of the four readings agreeing on a wrong
    number still goes red.

    The settlement clause is the one with teeth. Under-offering was argued to be
    "the safe direction"; it is not. A ceiling BELOW the settlement set is what
    makes the shipped surplus copy untrue, and a ceiling ABOVE it turns a
    coworker's unpaid debt into the payer's wallet balance while the driver
    believes the office is settled.

    A person the factory says has no row is asserted to HAVE no row — the
    negative half of the same invariant, and the reason ``zero_everything`` and
    ``debt_outside_place_only`` are walked too.

    GATE OFF IS NOT A SMALLER VERSION OF GATE ON. It is a different, equally
    binding contract: the row, the statement, the offer and the settlement all
    become the person's OWN cluster debt, no place is widened, no debt-free
    coworker is synthesised, and the post carries no ``delivery_address_id``.
    Running the same walk under both states is what proves the rollback switch
    moves the money and not merely the payload. It also used to be what showed
    the grocery defect was gate-scoped rather than unconditional; that defect is
    fixed, and `gate_on-grocery_at_place-mart` is now an ordinary green case
    like every other.
    """
    gate(gate_on)
    s = build(db, scenario_name)
    expected = s.expect(person_key)
    snapshot = money(db, s)

    if gate_on:
        # A7: the row carries the grouped place's whole debt, and a debt-free
        # coworker is synthesised onto the list.
        row_present = expected.expected_row_present
        row_total = expected.expected_row_total
        settleable = expected.engine_settleable_total
    else:
        # Plan D: the engine's own person rows, un-widened, nothing synthesised.
        # Cluster and personal scope settle the same set here because the
        # cluster's own delivered debt IS the personal ring for an unlinked
        # account and the whole ring for a linked one.
        row_present = expected.cluster_delivered_debt_count > 0
        row_total = expected.cluster_delivered_outstanding
        settleable = expected.cluster_delivered_outstanding

    journey.open_debtor_list()
    rows = {
        b.text: int(b.callback_data.rsplit("_", 1)[-1])
        for row in journey.last_markup.inline_keyboard
        for b in row
        if b.callback_data.startswith("staff_cod_customer_")
    }
    mine = {
        label: uid for label, uid in rows.items() if uid in expected.cluster_user_ids
    }

    if not row_present:
        assert not mine, (
            f"{scenario_name}/{person_key} has nothing collectible under this "
            f"gate state ({row_total}) yet has a row: {list(mine)}"
        )
        return

    # ⚠️ ONE PERSON, ONE ROW: a linked cluster is keyed on the account with the
    # largest own debt, so a debt-free sibling is present WITHOUT a row bearing
    # her own id. Match on the cluster, never on `user_id`.
    assert len(mine) == 1, (
        f"{scenario_name}/{person_key} expected exactly one row, got {list(mine)}"
    )
    label, customer_id = next(iter(mine.items()))
    row_figure = _figures(label)[0]

    statement = journey.tap_person(customer_id)
    screen_figure = _figure_labelled(statement, "COLLECTIBLE NOW")
    prompt = journey.press_collect_full(customer_id)
    assert prompt is not None, (
        f"{scenario_name}/{person_key}: the row advertised {row_figure} and the "
        "collect flow refused it outright"
    )
    offered = _figure_labelled(prompt, "ABOUT TO COLLECT")

    assert row_figure == screen_figure == offered, (
        f"{scenario_name}/{person_key}: row {row_figure} / statement "
        f"{screen_figure} / offer {offered} — three readings, three numbers"
    )
    assert row_figure == row_total, (
        f"{scenario_name}/{person_key}: the rendered row says {row_figure}, the "
        f"oracle says {row_total}"
    )
    assert offered == settleable, (
        f"{scenario_name}/{person_key}: offered {offered} but the engine will "
        f"settle {settleable}"
    )
    # The scope posted must degrade with the gate, not merely the figure — the
    # P0-degraded defect was a cluster-sized ceiling over a place-scoped post.
    journey.type_note("walked the whole journey")
    if not gate_on:
        assert journey.posted_collection()["delivery_address_id"] is None

    assert snapshot.total_settled() == offered, (
        f"{scenario_name}/{person_key}: offered {offered} but only "
        f"{snapshot.total_settled()} of debt moved; "
        f"{snapshot.total_credit_delta()} became prepaid credit"
    )
    assert snapshot.credit_delta() == {}, (
        f"{scenario_name}/{person_key}: a collection sized to the published "
        f"ceiling created credit {snapshot.credit_delta()}"
    )
    _assert_conservation(snapshot, offered)

    # And the loop closes: a collection sized to the published ceiling leaves
    # nothing collectible, so the screen the driver returns to says zero. If it
    # still advertises money, either the ceiling or the settlement was wrong.
    assert _figure_labelled(
        journey.tap_person(customer_id), "COLLECTIBLE NOW"
    ) == Decimal("0")


class TestGrocery:
    """``grocery_at_place`` — the one divergence the factory refused to judge.

    Its report said so explicitly: ``collect_scope_amount`` 18 000 vs
    ``engine_settleable_total`` 8 000 was "published as data on both sides rather
    than judged here… Deciding which of the two is wrong belongs to a stream
    that drives the screen, not to the fixture."

    This is that stream, so here is the ruling. A grocery's cash is mirrored
    onto a corporate contract and may never co-mingle — that backstop is
    deliberate and load-bearing, so the ENGINE's 8 000 is right and the SCREEN's
    18 000 was the defect.

    🔴 THESE TESTS WERE WRITTEN AS ``xfail(strict=True)`` STATING THE INTENDED
    CONTRACT, NOT THE MEASURED BEHAVIOUR — so fixing the defect flipped them to
    pass and the markers came off unchanged. What closed them: the display no
    longer restates the engine's rule, it ASKS
    (``cod_collect_ceiling.place_widening_applies`` →
    ``CashCollectionService.resolve_allocation_scope``, under the same
    ``STANDALONE_MEETING`` source the collect flow posts with) and widens only
    when the answer is PLACE. One decision, not two that agree.

    ``test_the_coworker_at_the_same_place_still_gets_the_whole_union`` is the
    other half of the ruling and the reason a blunter fix is wrong: the refusal
    is per-ACCOUNT, so a blanket "no widening at a place with a grocery in it"
    would satisfy the first two tests and silently break the third.
    """

    def test_the_offer_is_what_the_engine_will_settle(self, db, journey, gate):
        """The figure half: every screen must read the engine's 8 000."""
        s = build(db, "grocery_at_place")
        mart = s.expect("mart")
        # The two numbers the factory used to publish side by side, restated so
        # this test names the disagreement it exists to resolve — they are ONE
        # number now, which is the whole fix.
        assert mart.collect_scope_amount == mart.engine_settleable_total == Decimal("8000.00")
        # The place's union is still a fact about the topology; it is simply not
        # this account's to be offered.
        assert mart.collectible_total == Decimal("18000.00")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Mart")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        journey.type_note("grocery paid at the plaza")

        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == mart.engine_settleable_total
        )
        assert snapshot.total_settled() == offered
        assert snapshot.credit_delta() == {}
        # The scope degrades WITH the figure — an address kept while the number
        # falls back is the P0-degraded defect in its own right.
        assert journey.posted_collection()["delivery_address_id"] is None

    def test_the_coworker_at_the_same_place_still_gets_the_whole_union(
        self, db, journey, gate
    ):
        """🔴 THE CAPABILITY THE FIX MUST NOT COST. The plaza's 18 000 is still
        collectible in one go — through Alice, an ordinary individual whom the
        engine DOES grant a place scope. The grocery's refusal is per-ACCOUNT.

        This is the test that fails for a fix written as "do not widen a place
        that contains a grocery", and passes only for one that asks the engine
        about the person actually posting.
        """
        s = build(db, "grocery_at_place")
        alice = s.expect("alice")
        assert alice.collect_scope_amount == alice.engine_settleable_total == Decimal("18000.00")
        snapshot = money(db, s)

        journey.open_debtor_list()
        label, customer_id = journey.row_for("Alice")
        statement = journey.tap_person(customer_id)
        prompt = journey.press_collect_full(customer_id)
        offered = _figure_labelled(prompt, "ABOUT TO COLLECT")
        journey.type_note("the coworker paid for the plaza")

        assert (
            _figures(label)[0]
            == _figure_labelled(statement, "COLLECTIBLE NOW")
            == offered
            == Decimal("18000")
        )
        assert journey.posted_collection()["delivery_address_id"] is not None
        assert snapshot.total_settled() == offered
        assert snapshot.credit_delta() == {}
        # Including the shop's own 8 000 — a place-scoped post settles ring 1
        # whoever owns it.
        assert _figure_labelled(
            journey.tap_person(s.user("mart").id), "COLLECTIBLE NOW"
        ) == Decimal("0")

    def test_the_same_debt_cannot_be_collected_twice(self, db, journey, gate):
        """The harm half, and the reason this was never merely cosmetic.

        MEASURED UNDER THE DEFECT: lap one collected 18 000 and settled 8 000.
        The receipt then said "10,000 STILL COLLECTIBLE", the list still carried
        a 10 000 row, and lap two offered 10 000, settled **nothing**, and raised
        Mart's prepaid credit to 20 000. Alice's 10 000 at the same place never
        moved. So a driver following the screens took real cash on every lap for
        a debt no lap could ever pay.

        The contract asserted here is the only sane one, and it is the one that
        holds now: after collecting everything the screens offered, there is
        nothing left to offer — and no cash has quietly turned into a wallet
        balance.
        """
        s = build(db, "grocery_at_place")
        snapshot = money(db, s)

        journey.open_debtor_list()
        _, customer_id = journey.row_for("Mart")
        journey.tap_person(customer_id)
        journey.press_collect_full(customer_id)
        receipt = journey.type_note("lap one")

        assert _figure_labelled(receipt, "STILL COLLECTIBLE") == Decimal("0")
        second = journey.tap_person(customer_id)
        assert _figure_labelled(second, "COLLECTIBLE NOW") == Decimal("0")
        assert snapshot.credit_delta() == {}
        # And the coworker's debt was never touched by the shop's cash — all
        # 10 000 of it is still there, still hers, and still collectible from
        # her. (Mart's own 8 000 is settled, so the plaza's union is now hers
        # alone.)
        assert _figure_labelled(
            journey.tap_person(s.user("alice").id), "COLLECTIBLE NOW"
        ) == Decimal("10000")
