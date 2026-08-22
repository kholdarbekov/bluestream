"""One driver's working day, driven through the real staff-bot dispatcher.

WHY THIS FILE EXISTS
--------------------
Everything a driver does to an order — see the pool, claim a stop, walk it
through its statuses, take the money at the door, close it out — is a JOURNEY.
Each step's handler is already unit-tested somewhere under ``tests/staff_bot/``,
and that is exactly the coverage that let a driver tap a live button and have
nothing happen: a handler tested in isolation cannot show you whether the button
that renders it is registered, whether the id it acts on is the id the driver
tapped, or what the backend actually received when the driver walked away
mid-flow.

So every test here starts from ``/start``, reads the buttons **off the messages
the bot really sent**, feeds those exact callback payloads back through
``Application.process_update``, and then asserts on the two things that are real:
what the driver would SEE, and what reached the BACKEND — endpoint and body,
byte for byte. The staff bot is a money surface; ``{'status': 'delivered',
'metadata': {'cash_collected': 120000.0}}`` is a cash-custody record, and a test
that only checks "the API client was called" would have let every one of the
amount bugs in this module's history through.

WHAT IS FAKED, AND NOTHING ELSE
-------------------------------
The three seams ``tests/staff_bot/ptb_harness.py`` owns: the Telegram transport,
``api_client._make_request``, and the bot's own SQL. The delivery endpoints are
served by :class:`FakeDeliveryDesk` below, which models the parts of
``/api/v1/staff/delivery`` the bot actually depends on (pool paging, the
already-taken 409, the active list). Everything between those seams — the
keyboards, the anchoring guard, the money formatters, the text router — is
production code.
"""

from __future__ import annotations

import importlib.util
import json
from math import ceil
from pathlib import Path

import pytest

from staff_bot.utils import route_card_state
from staff_bot.utils.formatters import format_currency

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# The endpoints, spelled the way api_client spells them
# ---------------------------------------------------------------------------
# Hard-coded rather than imported from `config`: these paths ARE the contract
# with business_app, and a test that rebuilds them from the same constant the
# client uses would keep passing through a rename that breaks production.

LOGIN = "/api/v1/staff/auth/login"
DELIVERY = "/api/v1/staff/delivery"
POOL = f"{DELIVERY}/pool"
ACTIVE = f"{DELIVERY}/active"


def accept_endpoint(delivery_id: int) -> str:
    return f"{DELIVERY}/accept/{delivery_id}"


def status_endpoint(delivery_id: int) -> str:
    return f"{DELIVERY}/{delivery_id}/status"


# ---------------------------------------------------------------------------
# Real copy, loaded from the seed script (never pasted by hand)
# ---------------------------------------------------------------------------
# Same technique as tests/staff_bot/test_staff_menu_routing_journey.py: the
# strings a driver reads come from `_curated_value`, the very function
# `seed_translations()` calls. Pasting them here would let a future copy edit
# leave this file asserting text production no longer ships.

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

# The reply-keyboard labels a driver's menu renders. Needed in every language:
# `StaffBot._resolve_tapped_label` sweeps ALL of them on every tap (a keyboard
# rendered before a language switch keeps sending the old copy), so the table a
# test supplies is also the table the router matches against — the same
# coupling production has.
MENU_KEYS = (
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
)

# Every string a driver could read on the screens these journeys touch, served
# with production's own wording. Some are asserted verbatim; the rest are here
# so a screen renders the sentence a driver would really see instead of i18n's
# humanised-key placeholder — which would let a wrong-but-plausible failure
# message pass a substring check, and makes every assertion diff readable.
DELIVERY_KEYS = (
    "staff.back",
    "staff.confirm",
    "staff.cancel",
    "staff.items",
    "staff.page",
    "staff.currency.uzs",
    "staff.common.not_available",
    "staff.session_expired",
    "staff.unauthorized",
    "staff.error_occurred",
    "staff.error.api.account_deactivated",
    "staff.error.api.conflict",
    "staff.error.api.service_unavailable",
    "staff.delivery.pool_empty",
    "staff.delivery.pool_title",
    "staff.delivery.pool_count",
    "staff.delivery.view_details",
    "staff.delivery.accept",
    "staff.delivery.confirm_accept",
    "staff.delivery.accepted_success",
    "staff.delivery.already_taken",
    "staff.delivery.not_found",
    "staff.delivery.navigate",
    "staff.delivery.navigate_text",
    "staff.delivery.open_maps",
    "staff.delivery.confirm_status",
    "staff.delivery.status_updated",
    "staff.delivery.status.assigned",
    "staff.delivery.status.picked_up",
    "staff.delivery.status.in_transit",
    "staff.delivery.status.arrived",
    "staff.delivery.status.delivered",
    "staff.delivery.status.failed",
    "staff.delivery.status.cancelled",
    "staff.delivery.select_fail_reason",
    "staff.delivery.marked_failed",
    "staff.delivery.fail_reason_label",
    "staff.delivery.reason.customer_unavailable",
    "staff.delivery.cash_collection",
    "staff.delivery.confirm_cash",
    "staff.delivery.edit_cash",
    "staff.delivery.no_cash_collected",
    "staff.delivery.enter_cash_amount",
    "staff.delivery.enter_partial_cash_reason",
    "staff.delivery.enter_no_cash_reason",
    "staff.delivery.invalid_amount",
    "staff.delivery.note_required",
    "staff.delivery.delivered_success",
    "staff.delivery.cash_recorded",
    "staff.delivery.payment.cash",
    "staff.route.all_done",
    "staff.route.card_header",
    "staff.route.start_this_stop",
    "staff.route.all_stops_button",
    "staff.route.refresh",
    "staff.route.updated_at",
    "staff.route.navigate_all",
    "staff.delivery.optimize_routes_button",
)


def copy_for(key: str, language: str = "en") -> str:
    """The string production seeds for ``key`` — the driver's actual words."""
    value = _SEED._curated_value(key, language)
    assert value, (
        f"{key} has no curated {language} value in scripts/seed_staff_translations.py — "
        "production would render a humanised placeholder for it"
    )
    return value


def translation_table() -> dict:
    """Menu labels per language; delivery copy shared across all of them.

    Delivery copy is registered under the BARE key so every language resolves
    to the same English string: these tests assert what was recorded and what
    was shown, not which language it was shown in, and the menu keys — the ones
    whose per-language shape actually drives routing — are still per-language.
    """
    table = {}
    for key in MENU_KEYS:
        for language in LANGUAGES:
            table[(language, key)] = copy_for(key, language)
    for key in DELIVERY_KEYS:
        table[key] = copy_for(key, "en")
    return table


# ---------------------------------------------------------------------------
# The delivery half of the backend
# ---------------------------------------------------------------------------


class FakeDeliveryDesk:
    """``/api/v1/staff/delivery`` as the staff bot experiences it.

    Deliberately models paging arithmetic rather than echoing a canned
    ``pagination`` block: the pool's stale-page clamp only means anything if
    ``pages`` is derived from how many orders EXIST, not from the page that was
    asked for — which is exactly the shape of the bug the clamp was added for.
    """

    def __init__(self, backend):
        self.backend = backend
        self.pool: list[dict] = []
        self.active: list[dict] = []
        self.accept_outcomes: dict[int, object] = {}
        self.status_outcomes: dict[int, object] = {}
        backend.route("GET", POOL, self._serve_pool)
        backend.route("GET", ACTIVE, self._serve_active)

    def serve(self, *delivery_ids: int):
        """Register the per-delivery write endpoints for these ids."""
        for delivery_id in delivery_ids:
            self.backend.route("POST", accept_endpoint(delivery_id), self._serve_accept)
            self.backend.route("PUT", status_endpoint(delivery_id), self._serve_status)

    # -- reads ---------------------------------------------------------------

    def _serve_pool(self, call):
        params = call.params or {}
        wanted = params.get("delivery_id")
        if wanted is not None:
            rows = [row for row in self.pool if row["delivery_id"] == wanted]
            return {"items": rows, "pagination": {"page": 1, "pages": 1, "total": len(rows)}}

        page = int(params.get("page") or 1)
        per_page = int(params.get("per_page") or 10)
        pages = max(1, ceil(len(self.pool) / per_page))
        start = (page - 1) * per_page
        return {
            "items": self.pool[start:start + per_page],
            "pagination": {"page": page, "pages": pages, "total": len(self.pool)},
        }

    def _serve_active(self, _call):
        return {"items": list(self.active), "route_summary": self.route_summary()}

    def route_summary(self) -> dict:
        return {
            "stops_total_today": len(self.active),
            "stops_completed_today": 0,
            "committed_delivery_id": None,
        }

    # -- writes --------------------------------------------------------------

    def _serve_accept(self, call):
        delivery_id = int(call.endpoint.rsplit("/", 1)[-1])
        return self.accept_outcomes.get(delivery_id, {"delivery_id": delivery_id})

    def _serve_status(self, call):
        delivery_id = int(call.endpoint.split("/")[-2])
        return self.status_outcomes.get(delivery_id, {"delivery_id": delivery_id})


def pool_row(delivery_id: int, order_number: str, **overrides) -> dict:
    row = {
        "delivery_id": delivery_id,
        "order_id": 9000 + delivery_id,
        "order_number": order_number,
        "customer_name": "Dilnoza Rashidova",
        "customer_phone": "+998901234567",
        "district": "Chilonzor",
        "address": "15 Bunyodkor shoh ko'chasi",
        "status": "pending",
        "payment_method": "cash",
        "total_amount": 150000,
        "expected_cash_to_collect": 150000,
        "item_count": 3,
    }
    row.update(overrides)
    return row


def active_row(delivery_id: int, order_number: str, **overrides) -> dict:
    row = {
        "delivery_id": delivery_id,
        "order_id": 9000 + delivery_id,
        "order_number": order_number,
        "customer_id": 700 + delivery_id,
        "customer_name": "Dilnoza Rashidova",
        "customer_phone": "+998901234567",
        "status": "assigned",
        "district": "Chilonzor",
        "address": "15 Bunyodkor shoh ko'chasi",
        "items": [{"product_name": "Bluestream 18.9L", "quantity": 3, "total_price": 150000}],
        "payment_method": "cash",
        "payment_status": "pending",
        "total_amount": 150000,
        "amount_collected": 0,
        "outstanding_amount": 150000,
        "expected_cash_to_collect": 150000,
        "cod_reserved_prepayment_amount": 0,
        # No empties on record: keeps the bottle-return step out of the way so
        # these tests are about the MONEY step. The bottle prompt has its own
        # file (tests/staff_bot/test_bottle_return_prompt.py).
        "expected_returnable_bottles": 0,
        "customer_bottle_balance": 0,
        "place_bottle_balance_signed": 0,
        "destination_latitude": 41.2876,
        "destination_longitude": 69.2224,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# A signed-in driver
# ---------------------------------------------------------------------------


def driver_row(language="en"):
    return {
        "id": 55,
        "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
        "first_name": "Aziz",
        "last_name": "Karimov",
        "phone": "+998901112233",
        "preferred_language": language,
        "role": "delivery",
        "status": "active",
        "staff_roles": json.dumps(["delivery_driver"]),
        "staff_bot_state": "{}",
    }


def login_payload(language="en"):
    return {
        "access_token": "staff-access-token",
        "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {
            "id": 55,
            "first_name": "Aziz",
            "last_name": "Karimov",
            "phone": "+998901112233",
            "preferred_language": language,
            "staff_roles": ["delivery_driver"],
            "delivery_person_id": 7,
        },
    }


@pytest.fixture(autouse=True)
def _isolated_route_card_state():
    """The route card's Redis seam off, and its per-driver locks fresh.

    ``route_card_state`` holds module-level ``asyncio.Lock``s keyed by driver.
    A lock first awaited in one test's event loop raises "bound to a different
    event loop" the moment the next test awaits it, so a stale entry here turns
    the SECOND route-card test in any file into a spurious failure. Redis is
    left unconfigured on purpose: that is production's documented degraded path
    and the one this harness runs in.
    """
    route_card_state.configure(None)
    route_card_state._locks.clear()
    yield
    route_card_state.configure(None)
    route_card_state._locks.clear()


async def build_driver(monkeypatch, *, language="en", login=None):
    harness = await build_staff_harness(
        monkeypatch,
        translations=translation_table(),
        database=FakeStaffDatabase(staff_user=driver_row(language)),
    )
    harness.backend.route(
        "POST", LOGIN, login if login is not None else (lambda _call: login_payload(language))
    )
    harness.desk = FakeDeliveryDesk(harness.backend)
    return harness


async def sign_in(harness):
    """Run the real ``/start`` login; return (update factory, menu labels)."""
    driver = harness.updates()
    await harness.send(driver.command("start"))

    shown = harness.telegram.shown
    assert shown, "/start produced no message at all — the driver sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    harness.telegram.reset()
    return driver, labels


def menu_label(labels: list[str], key: str) -> str:
    """The ONE rendered reply-keyboard label carrying ``key``'s translation.

    Matched on the translated value rather than rebuilt as f"{emoji} {value}",
    so the emoji stays an implementation detail of the keyboard.
    """
    value = copy_for(key, "en")
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, f"expected exactly one menu button carrying {value!r}, got {hits}"
    return hits[0]


def capture_errors(harness) -> list:
    """Every exception PTB would have swallowed into its logs.

    Without this a handler that raises is indistinguishable from one that
    quietly did nothing — and from the driver's side, both are a spinner.
    """
    errors = []

    async def _record(_update, context):
        errors.append(context.error)

    harness.application.add_error_handler(_record)
    return errors


# -- reading what happened ---------------------------------------------------


def backend_calls(harness, method=None, endpoint=None):
    return [
        call
        for call in harness.backend.calls
        if (method is None or call.method == method)
        and (endpoint is None or call.endpoint == endpoint)
    ]


def status_writes(harness) -> list[tuple[str, dict]]:
    """Every delivery-status PUT, as (endpoint, body)."""
    return [
        (call.endpoint, call.data)
        for call in harness.backend.calls
        if call.method == "PUT" and call.endpoint.endswith("/status")
    ]


def texts(harness) -> list[str]:
    return [call.text for call in harness.telegram.shown]


def toasts(harness) -> list[str]:
    return [call.params.get("text", "") for call in harness.telegram.of("answerCallbackQuery")]


def reject_repeat_callback_answers(harness):
    """Let the FIRST answerCallbackQuery through and reject every one after it.

    That is Telegram's real behaviour — a callback query may be answered once —
    and several handlers here answer immediately and then try to answer again
    with an error alert. Scripting it is the only way to find out whether the
    driver still learns what went wrong.
    """
    seen = {"count": 0}

    def _answer(_params):
        seen["count"] += 1
        if seen["count"] == 1:
            return 200, {"ok": True, "result": True}
        return 400, {
            "ok": False,
            "error_code": 400,
            "description": (
                "Bad Request: query is too old and response timeout expired "
                "or query ID is invalid"
            ),
        }

    harness.telegram.failures["answerCallbackQuery"] = _answer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def bot(monkeypatch):
    return await build_driver(monkeypatch)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


async def test_an_empty_pool_says_so_instead_of_leaving_the_driver_staring_at_nothing(bot):
    """A driver taps "New Orders" during a quiet hour.

    If this fails the driver gets silence, and silence from a reply-keyboard tap
    is indistinguishable from a crashed bot — there is no spinner and no toast
    on that surface, so they tap again, and again. The Back button matters for
    the same reason: an empty screen with no way out is a dead end.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = []
    errors = capture_errors(bot)

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))

    assert not errors, f"opening an empty pool raised {errors}"
    shown = bot.telegram.shown
    assert shown, "an empty pool showed the driver nothing at all"
    assert shown[-1].text == f"📦 {copy_for('staff.delivery.pool_empty')}"
    assert shown[-1].callback_data() == ["staff_back_to_main"], (
        "the empty-pool screen must offer a way back; a dead end reads as a broken bot"
    )
    assert backend_calls(bot, "GET", POOL)[0].params == {"page": 1, "per_page": 10}


async def test_the_pool_gives_every_order_its_own_view_and_accept_buttons(bot):
    """The pool is one message per order, and each carries the id of ITS order.

    A driver scrolling a pool of look-alike cards is relying entirely on the
    callback id being right. If two cards ever shared one id — a loop variable
    captured late, a delivery_id/order_id mix-up — the driver would claim an
    order they never looked at, at an address they never read.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = [pool_row(501, "BS-1001"), pool_row(502, "BS-1002", district="Yunusobod")]

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))

    shown = bot.telegram.shown
    header, *cards = shown
    assert header.text == (
        f"📦 <b>{copy_for('staff.delivery.pool_title')}</b>\n"
        + copy_for("staff.delivery.pool_count").format(count=2)
        + "\n"
    )
    assert [card.callback_data() for card in cards] == [
        ["staff_view_order_501", "staff_accept_order_501"],
        ["staff_view_order_502", "staff_accept_order_502"],
    ]
    assert "#BS-1001" in cards[0].text and "#BS-1002" in cards[1].text
    assert "Yunusobod" in cards[1].text, "each card must describe its OWN order"


async def test_reopening_the_pool_from_the_menu_starts_at_page_one_again(bot):
    """The give-up-and-come-back path, and the stale page button behind it.

    A driver pages to 2, then other drivers empty the pool down to one page. Two
    things must hold or that driver stops seeing work at all: reopening the menu
    must reset them to page 1 (the reported bug: ``pool_page`` was sticky, so a
    driver who once paged forward saw an empty list forever while colleagues on
    page 1 saw the orders), and a page-2 button left on an older message must
    clamp back into range rather than render a dead-end empty page.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = [pool_row(500 + n, f"BS-10{n:02d}") for n in range(1, 16)]
    new_orders = menu_label(labels, "staff.menu.new_orders")

    await bot.send(driver.text(new_orders))
    await bot.send(driver.tap("staff_pool_page_2"))
    assert any("#BS-1011" in call.text for call in bot.telegram.shown), (
        "fixture: paging forward should have shown the second page"
    )

    # The driver walks away, comes back, and taps the menu again — while the
    # pool is still two pages long, so nothing but the reset can bring them home.
    bot.telegram.reset()
    await bot.send(driver.text(new_orders))
    assert any("#BS-1001" in call.text for call in bot.telegram.shown), (
        "reopening New Orders must start at page 1 — a sticky pool_page hides "
        "every order from the driver who once paged forward"
    )

    # Colleagues claim ten of the fifteen; the pool is one page again, and the
    # driver still has a page-2 button sitting on an older message.
    await bot.send(driver.tap("staff_pool_page_2"))
    bot.desk.pool = bot.desk.pool[:5]

    bot.telegram.reset()
    await bot.send(driver.tap("staff_pool_page_2"))
    clamped = bot.telegram.shown
    assert clamped, "a stale page-2 tap showed nothing"
    assert any("#BS-1001" in call.text for call in clamped), (
        "a page beyond the shrunken pool must clamp back to page 1, not render empty"
    )

    assert [call.params for call in backend_calls(bot, "GET", POOL)] == [
        {"page": 1, "per_page": 10},   # menu tap
        {"page": 2, "per_page": 10},   # pagination tap
        {"page": 1, "per_page": 10},   # fresh menu entry resets the page
        {"page": 2, "per_page": 10},   # paged forward again, still two pages
        {"page": 2, "per_page": 10},   # stale page-2 tap after the pool shrank
        {"page": 1, "per_page": 10},   # clamped back into range and refetched
    ]


async def test_claiming_an_order_posts_exactly_one_claim_and_leaves_the_menu_alone(bot):
    """The happy path, asserted at the wire.

    Two properties, both of which have been broken here before. The claim is ONE
    POST — ``accept`` is excluded from the client's retry-safe verbs precisely
    because a replayed claim is a second driver's order taken away. And the
    success screen sends no new message: accepting used to follow up with a
    location prompt whose reply keyboard REPLACED the driver's whole main menu,
    on every single accept.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = [pool_row(501, "BS-1001")]
    bot.desk.serve(501)

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))
    await bot.send(driver.tap("staff_accept_order_501"))

    confirm = bot.telegram.last_shown()
    assert confirm.text == copy_for("staff.delivery.confirm_accept")
    assert confirm.callback_data() == ["staff_confirm_accept_501", "staff_new_orders"]

    bot.telegram.reset()
    await bot.send(driver.tap("staff_confirm_accept_501"))

    claims = backend_calls(bot, "POST", accept_endpoint(501))
    assert len(claims) == 1, f"expected exactly one claim POST, got {claims}"
    assert claims[0].data is None, "the claim carries no body; the id is the whole request"

    done = bot.telegram.last_shown()
    assert done.text == f"✅ {copy_for('staff.delivery.accepted_success')}"
    assert done.callback_data() == ["staff_active_deliveries"]
    assert bot.telegram.of("sendMessage") == [], (
        "accepting an order must not send a second message — the one that used to "
        "follow carried a reply keyboard that wiped the driver's main menu"
    )


async def test_claiming_an_order_another_driver_already_took_refuses_and_disarms_the_button(bot):
    """Two drivers, one order, one van already on its way.

    The pool card on this driver's phone was rendered before the other driver
    claimed it, so the backend 409 is the ONLY thing standing between them and a
    double-assigned stop. The refusal must also take the Confirm button away:
    leaving it on screen invites the frustrated re-tap that turns one refused
    claim into a burst of them against a live endpoint.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = [pool_row(501, "BS-1001")]
    bot.desk.serve(501)
    bot.desk.accept_outcomes[501] = staff_backend_failure(
        "This delivery has already been accepted by another driver",
        status_code=409,
        error_code="STAFF_DELIVERY_ALREADY_TAKEN",
    )

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))
    await bot.send(driver.tap("staff_accept_order_501"))
    bot.telegram.reset()
    await bot.send(driver.tap("staff_confirm_accept_501"))

    refusal = bot.telegram.last_shown()
    assert refusal.text == f"❌ {copy_for('staff.delivery.already_taken')}"
    assert refusal.callback_data() == ["staff_new_orders"], (
        "the refusal screen still offers Confirm — a stale claim button on a "
        "screen that just said 'already taken' is an invitation to hammer it"
    )
    assert len(backend_calls(bot, "POST", accept_endpoint(501))) == 1


async def test_a_driver_deactivated_mid_shift_is_told_why_and_claims_nothing(bot):
    """Dispatch switches a driver off while their pool list is still on screen.

    ``require_delivery_driver`` cannot catch this: the driver's roles were loaded
    at login and the keyboard lives on their phone, so the backend's
    STAFF_ACCOUNT_DEACTIVATED is the only signal. They must read the real reason
    — a generic "conflict" or a silent screen sends them to dispatch confused,
    or, worse, to the customer's door with an order the system never gave them.
    """
    driver, labels = await sign_in(bot)
    bot.desk.pool = [pool_row(501, "BS-1001")]
    bot.desk.serve(501)
    bot.desk.accept_outcomes[501] = staff_backend_failure(
        "Access denied", status_code=403, error_code="STAFF_ACCOUNT_DEACTIVATED"
    )

    await bot.send(driver.text(menu_label(labels, "staff.menu.new_orders")))
    await bot.send(driver.tap("staff_accept_order_501"))
    bot.telegram.reset()
    await bot.send(driver.tap("staff_confirm_accept_501"))

    refusal = bot.telegram.last_shown()
    assert refusal.text == f"❌ {copy_for('staff.error.api.account_deactivated')}"
    assert refusal.callback_data() == ["staff_new_orders"]


async def test_a_deactivated_driver_still_learns_why_when_telegram_drops_the_toast(bot):
    """The deactivation notice has to survive Telegram's one-answer rule.

    ``execute_status_change`` acknowledges the tap immediately (callback ids
    expire in seconds), so when the backend then refuses, the error arrives as a
    SECOND answer to the same query — which Telegram rejects outright. If that
    rejection were the end of it, a deactivated driver would tap "Picked up",
    see the spinner stop, see nothing change, and keep driving. The fallback
    reply is what turns a swallowed toast into a message they can read.
    """
    driver, labels = await sign_in(bot)
    bot.desk.active = [active_row(501, "BS-1001")]
    bot.desk.serve(501)
    bot.desk.status_outcomes[501] = staff_backend_failure(
        "Access denied", status_code=403, error_code="STAFF_ACCOUNT_DEACTIVATED"
    )
    errors = capture_errors(bot)

    await bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    await bot.send(driver.tap("staff_view_active_501"))
    await bot.send(driver.tap("staff_status_501_picked_up"))

    reject_repeat_callback_answers(bot)
    bot.telegram.reset()
    await bot.send(driver.tap("staff_execute_status_501_picked_up"))

    assert not errors, f"a rejected toast escaped as a handler error: {errors}"
    expected = f"❌ {copy_for('staff.error.api.account_deactivated')}"
    assert expected in toasts(bot), (
        "the refusal was never even attempted as a toast — this test would then "
        "be proving nothing about the fallback below it"
    )
    assert expected in texts(bot), (
        "Telegram refused the second answer to this callback and nothing else "
        f"reached the driver; they saw {texts(bot)}"
    )
    assert not backend_calls(bot, "PUT", status_endpoint(501))[1:], (
        "the refused status change was retried; a deactivated driver's tap must "
        "hit the backend once and stop"
    )
    assert not any(copy_for("staff.delivery.status_updated").split("{")[0] in text
                   for text in texts(bot)), "a refused status change must never report success"


# ---------------------------------------------------------------------------
# The stop itself
# ---------------------------------------------------------------------------


async def open_the_stop(harness):
    """Sign in, open the route card, and open the one stop on it.

    Returns (update factory, the rendered stop-detail call). Every at-door test
    below starts from here because that is where a driver starts: the card is
    the only long-lived surface they have.
    """
    driver, labels = await sign_in(harness)
    await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    card = harness.telegram.last_shown()
    assert copy_for("staff.route.card_header").format(current=1, total=1) in card.text, (
        f"the route card did not render its header; it said {card.text!r}"
    )
    assert card.callback_data() == [
        "staff_view_active_501",
        "staff_route_view_all",
        "staff_optimize_routes",
        "staff_route_refresh",
    ]

    harness.telegram.reset()
    await harness.send(driver.tap("staff_view_active_501"))
    return driver, harness.telegram.last_shown()


async def walk_to_the_door(harness, driver):
    """picked_up → in_transit → arrived, each through its confirm screen."""
    for status in ("picked_up", "in_transit", "arrived"):
        await harness.send(driver.tap(f"staff_status_501_{status}"))
        await harness.send(driver.tap(f"staff_execute_status_501_{status}"))


@pytest.fixture
async def stop_bot(monkeypatch):
    harness = await build_driver(monkeypatch)
    harness.desk.active = [active_row(501, "BS-1001")]
    harness.desk.serve(501)
    return harness


async def test_a_delivery_walks_to_the_door_one_status_at_a_time(stop_bot):
    """The status ladder, tapped the way a driver taps it.

    Each rung is confirm-then-execute, and the keyboard after each execute must
    already offer the NEXT rung — otherwise the driver has to navigate back to
    the card between every step, which on a phone in a moving van is how stops
    get marked in the wrong order or not at all.

    The last assertion is the one that matters most: arriving at the door writes
    NOTHING. "Delivered" opens the cash screen instead, so an order with money
    on it can never be closed before the money is accounted for.
    """
    driver, detail = await open_the_stop(stop_bot)
    errors = capture_errors(stop_bot)

    assert detail.callback_data() == [
        "staff_status_501_picked_up",
        "staff_status_501_failed",
        "staff_status_501_cancelled",
        "staff_navigate_501",
        "staff_active_deliveries",
    ]

    await stop_bot.send(driver.tap("staff_status_501_picked_up"))
    confirm = stop_bot.telegram.last_shown()
    assert copy_for("staff.delivery.confirm_status").format(
        status=f"📦 {copy_for('staff.delivery.status.picked_up')}"
    ) in confirm.text
    assert confirm.callback_data() == [
        "staff_execute_status_501_picked_up",
        "staff_view_active_501",
    ], "the confirm screen must be able to back out to the stop it came from"

    await stop_bot.send(driver.tap("staff_execute_status_501_picked_up"))
    after = stop_bot.telegram.last_shown()
    assert copy_for("staff.delivery.status_updated").format(
        status=f"📦 {copy_for('staff.delivery.status.picked_up')}"
    ) in after.text
    assert "staff_status_501_in_transit" in after.callback_data(), (
        "after picking up, the next rung must be one tap away on the same message"
    )

    await stop_bot.send(driver.tap("staff_status_501_in_transit"))
    await stop_bot.send(driver.tap("staff_execute_status_501_in_transit"))
    await stop_bot.send(driver.tap("staff_status_501_arrived"))
    await stop_bot.send(driver.tap("staff_execute_status_501_arrived"))

    assert not errors, f"walking the status ladder raised {errors}"
    assert status_writes(stop_bot) == [
        (status_endpoint(501), {"status": "picked_up"}),
        (status_endpoint(501), {"status": "in_transit"}),
        (status_endpoint(501), {"status": "arrived"}),
    ]

    stop_bot.telegram.reset()
    await stop_bot.send(driver.tap("staff_status_501_delivered"))

    assert len(status_writes(stop_bot)) == 3, (
        "reaching the door wrote a 'delivered' status before the cash was "
        "accounted for; the money screen is the gate"
    )
    prompt = stop_bot.telegram.last_shown()
    assert copy_for("staff.delivery.cash_collection").format(
        amount=format_currency(150000, language="en")
    ) in prompt.text
    assert prompt.callback_data() == [
        "staff_cash_full_501",
        "staff_cash_partial_501",
        "staff_cash_none_501",
    ]


async def test_the_cash_screen_records_what_the_driver_counted_not_what_was_owed(stop_bot):
    """The customer is 30 000 short and promises the rest next week.

    This is the whole reason the "Edit cash amount" branch exists, and the
    number the driver types is a cash-custody record: it becomes the driver's
    expected cash on hand at reconciliation, and the customer's remaining debt.
    If the submitted ``cash_collected`` ever fell back to the amount OWED, the
    driver would be held personally short by 30 000 at end of shift and the
    customer's debt would silently vanish. The audit note is mandatory for the
    same reason — an unexplained shortfall is indistinguishable from theft.
    """
    driver, _detail = await open_the_stop(stop_bot)
    await walk_to_the_door(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_status_501_delivered"))

    stop_bot.telegram.reset()
    await stop_bot.send(driver.tap("staff_cash_partial_501"))
    ask_amount = stop_bot.telegram.last_shown()
    assert ask_amount.text == copy_for("staff.delivery.enter_cash_amount")
    assert ask_amount.callback_data() == ["staff_flow_cancel"], (
        "a free-text prompt with no Cancel traps the driver: the text router "
        "eats every reply-keyboard tap while the flow is armed"
    )

    await stop_bot.send(driver.text("120000"))
    assert stop_bot.telegram.last_shown().text == copy_for(
        "staff.delivery.enter_partial_cash_reason"
    )
    assert len(status_writes(stop_bot)) == 3, "the amount alone must not close the delivery"

    note = "Customer 30 000 short, will settle on the next drop"
    stop_bot.telegram.reset()
    await stop_bot.send(driver.text(note))

    assert status_writes(stop_bot)[-1] == (
        status_endpoint(501),
        {"status": "delivered", "metadata": {"cash_collected": 120000.0, "notes": note}},
    )
    closing = stop_bot.telegram.last_shown()
    assert f"✅ {copy_for('staff.delivery.delivered_success')}" in closing.text
    assert copy_for("staff.delivery.cash_recorded").format(
        amount=format_currency(120000, language="en")
    ) in closing.text
    assert note in closing.text, "the driver must see the note that was filed under their name"


async def test_a_cash_amount_that_is_not_money_is_refused_and_writes_nothing(stop_bot):
    """Fat fingers at a doorway, with a phone in one hand and a bottle in the other.

    Every one of these has to be refused WITHOUT ending the flow: a rejected
    amount that also tore the flow down would leave the driver at a completed
    door with a delivery still open and no screen to finish it from. And nothing
    may reach the backend until a real number arrives — ``nan`` and ``inf`` are
    listed because ``float()`` accepts both and neither is caught by a sign
    check, so they would have been posted as JSON literals into a money field.
    """
    driver, _detail = await open_the_stop(stop_bot)
    await walk_to_the_door(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_status_501_delivered"))
    await stop_bot.send(driver.tap("staff_cash_partial_501"))

    for typed in ("yuz yigirma ming", "-5000", "0", "nan", "Infinity"):
        stop_bot.telegram.reset()
        await stop_bot.send(driver.text(typed))
        assert texts(stop_bot) == [copy_for("staff.delivery.invalid_amount")], (
            f"{typed!r} was not refused as an amount; the driver saw {texts(stop_bot)}"
        )
        assert len(status_writes(stop_bot)) == 3, f"{typed!r} reached the backend"

    await stop_bot.send(driver.text("120 000"))
    assert stop_bot.telegram.last_shown().text == copy_for(
        "staff.delivery.enter_partial_cash_reason"
    ), "the flow did not survive the rejected attempts — a valid amount must still land"

    await stop_bot.send(driver.text("Counted at the door"))
    assert status_writes(stop_bot)[-1][1]["metadata"]["cash_collected"] == 120000.0, (
        "a thousands separator is how a human writes money and must parse"
    )


async def test_a_zero_cash_delivery_cannot_be_filed_without_a_written_reason(stop_bot):
    """"They didn't pay" is a claim about money, so it has to be signed.

    The zero-cash branch closes a delivery whose customer owed 150 000 and
    handed over nothing. Recorded silently it is unauditable — nobody can later
    tell a refusal at the door from cash that never reached the office. So a
    blank note is refused, the flow stays open, and the eventual write carries
    both the zero and the words.
    """
    driver, _detail = await open_the_stop(stop_bot)
    await walk_to_the_door(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_status_501_delivered"))

    stop_bot.telegram.reset()
    await stop_bot.send(driver.tap("staff_cash_none_501"))
    assert stop_bot.telegram.last_shown().text == copy_for("staff.delivery.enter_no_cash_reason")

    stop_bot.telegram.reset()
    await stop_bot.send(driver.text("   "))
    assert texts(stop_bot) == [copy_for("staff.delivery.note_required")]
    assert len(status_writes(stop_bot)) == 3, "a blank reason closed the delivery anyway"

    reason = "Customer refused to pay, asked to call the office"
    await stop_bot.send(driver.text(reason))
    assert status_writes(stop_bot)[-1] == (
        status_endpoint(501),
        {"status": "delivered", "metadata": {"cash_collected": 0.0, "notes": reason}},
    )


async def test_a_failed_delivery_records_the_reason_the_driver_picked(stop_bot):
    """Nobody home. The delivery ends, and WHY it ended is the whole record.

    ``fail_reason`` drives re-dispatch and the customer's notification, so a
    reason that arrived as the wrong string — or a screen that fired the write
    before the driver had chosen one — turns a re-deliverable order into a dead
    one. The confirmation names the reason back so the driver can catch a
    mis-tap while they are still outside the door.
    """
    driver, _detail = await open_the_stop(stop_bot)
    errors = capture_errors(stop_bot)

    stop_bot.telegram.reset()
    await stop_bot.send(driver.tap("staff_status_501_failed"))

    chooser = stop_bot.telegram.last_shown()
    assert copy_for("staff.delivery.select_fail_reason") in chooser.text
    assert chooser.callback_data() == [
        "staff_failed_reason_501_customer_unavailable",
        "staff_failed_reason_501_wrong_address",
        "staff_failed_reason_501_customer_refused",
        "staff_failed_reason_501_product_damaged",
        "staff_failed_reason_501_other",
        "staff_view_active_501",
    ]
    assert status_writes(stop_bot) == [], "choosing a reason must come BEFORE the write"

    stop_bot.telegram.reset()
    await stop_bot.send(driver.tap("staff_failed_reason_501_customer_unavailable"))

    assert not errors, f"failing a delivery raised {errors}"
    assert status_writes(stop_bot) == [
        (status_endpoint(501),
         {"status": "failed", "metadata": {"fail_reason": "customer_unavailable"}}),
    ]
    closing = stop_bot.telegram.last_shown()
    assert f"❌ {copy_for('staff.delivery.marked_failed')}" in closing.text
    assert (
        f"{copy_for('staff.delivery.fail_reason_label')}: "
        f"{copy_for('staff.delivery.reason.customer_unavailable')}"
    ) in closing.text
    assert closing.callback_data() == ["staff_active_deliveries"]


# ---------------------------------------------------------------------------
# Buttons that outlived the screen that drew them
# ---------------------------------------------------------------------------


@pytest.fixture
async def two_stop_bot(monkeypatch):
    """A driver holding two live stops — different money, different doors.

    Every distinguishing value differs on purpose: an assertion that cannot tell
    stop A from stop B proves nothing about which one a stale button acted on.
    """
    harness = await build_driver(monkeypatch)
    harness.desk.active = [
        active_row(501, "BS-1001", expected_cash_to_collect=150000, total_amount=150000,
                   destination_latitude=41.2876, destination_longitude=69.2224),
        active_row(502, "BS-2002", customer_name="Sardor Yo'ldoshev",
                   district="Yunusobod", expected_cash_to_collect=48000,
                   total_amount=48000, outstanding_amount=48000,
                   address="7 Amir Temur ko'chasi",
                   destination_latitude=41.3450, destination_longitude=69.2870),
    ]
    harness.desk.serve(501, 502)
    return harness


async def test_a_stale_cash_button_pays_down_the_order_it_belongs_to(two_stop_bot):
    """Every stop is its own chat message; the driver has ONE ``current_delivery``.

    So a driver who opens stop B and then scrolls back to A's older — still
    perfectly live — screen is acting on A through a snapshot that says B. That
    is not hypothetical: it is the bug the re-anchoring guard was written for,
    and its symptom was B's money being collected against A's order.

    The tapped id is the truth. Here A owes 150 000 and B owes 48 000, so if the
    guard ever regressed, the number posted would be visibly the other order's.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    # Work stop A up to its door, then get distracted and open stop B.
    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await walk_to_the_door(two_stop_bot, driver)
    await two_stop_bot.send(driver.tap("staff_status_501_delivered"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    two_stop_bot.telegram.reset()
    # …and now tap "Confirm cash" on A's older, still-live screen.
    await two_stop_bot.send(driver.tap("staff_cash_full_501", message_id=240))

    assert status_writes(two_stop_bot)[-1] == (
        status_endpoint(501),
        {"status": "delivered", "metadata": {"cash_collected": 150000.0}},
    ), "the tapped order's own money must be what is recorded, against its own delivery"
    assert not any(
        endpoint == status_endpoint(502) for endpoint, _body in status_writes(two_stop_bot)
    ), "stop B was written to by a tap on stop A's card"


async def test_a_stale_button_for_a_delivery_that_is_gone_refuses_instead_of_guessing(two_stop_bot):
    """The same stale tap, but the order is no longer the driver's.

    Dispatch reassigned it while the driver was looking at the other stop. There
    is no correct order to act on now, and the dangerous failure is acting on
    whichever snapshot happens to be loaded — which would post THIS stop's money
    against a delivery the driver never touched. Refusing and sending them back
    to the card is the only safe answer.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await walk_to_the_door(two_stop_bot, driver)
    await two_stop_bot.send(driver.tap("staff_status_501_delivered"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    # Dispatch takes stop A away.
    two_stop_bot.desk.active = [row for row in two_stop_bot.desk.active
                                if row["delivery_id"] != 501]
    before = len(status_writes(two_stop_bot))

    two_stop_bot.telegram.reset()
    await two_stop_bot.send(driver.tap("staff_cash_full_501", message_id=240))

    refusal = two_stop_bot.telegram.last_shown()
    assert refusal.text == copy_for("staff.delivery.not_found")
    assert refusal.callback_data() == ["staff_active_deliveries"]
    assert len(status_writes(two_stop_bot)) == before, (
        "a tap on a delivery that is no longer the driver's wrote a status anyway"
    )


async def test_a_stale_failed_reason_tap_names_the_order_it_was_tapped_on(two_stop_bot):
    """A failure reason picked on an older card closes the order it belongs to.

    Every at-door handler re-anchors ``current_delivery`` on the id in the
    callback before it reads the snapshot (``_anchor_current_delivery``);
    ``select_fail_reason`` was the one exception. So when a driver picked a
    failure reason on stop A's older screen while stop B was the one they last
    opened, the PUT correctly targeted A (the id rides the callback) but the
    confirmation was titled with B's order number, and B's cached snapshot was
    stamped ``status='failed'``. The driver read "#BS-2002 — failed" for an
    order that was alive and waiting, and B's own card was then titled from a
    corrupted snapshot.

    The guard now runs here too, so all three — the write, the words on the
    screen, and the snapshot left behind — belong to the tapped stop.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await two_stop_bot.send(driver.tap("staff_status_501_failed"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    two_stop_bot.telegram.reset()
    await two_stop_bot.send(
        driver.tap("staff_failed_reason_501_customer_unavailable", message_id=240)
    )

    assert status_writes(two_stop_bot) == [
        (status_endpoint(501),
         {"status": "failed", "metadata": {"fail_reason": "customer_unavailable"}}),
    ], "the write must land on the tapped delivery, and on nothing else"

    closing = two_stop_bot.telegram.last_shown()
    assert "#BS-1001" in closing.text, (
        "the driver was shown a different order number than the stop they failed"
    )
    assert "#BS-2002" not in closing.text, (
        "the other stop is named on the screen that closed this one"
    )
    snapshot = two_stop_bot.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["current_delivery"]
    assert snapshot["delivery_id"] == 501 and snapshot["status"] == "failed", (
        "the failed stamp landed on the other stop's cached snapshot"
    )


async def test_a_failed_reason_tap_for_a_delivery_that_is_gone_refuses_instead_of_guessing(
    two_stop_bot,
):
    """Dispatch took the stop away while the driver was on the other card.

    There is no correct order to fail now, and marking whichever snapshot is
    loaded would close a stop the driver never stood in front of. Refusing and
    sending them back to the list is the only safe answer — the same one
    ``confirm_full_cash_collection`` already gives.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await two_stop_bot.send(driver.tap("staff_status_501_failed"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    two_stop_bot.desk.active = [row for row in two_stop_bot.desk.active
                                if row["delivery_id"] != 501]
    before = len(status_writes(two_stop_bot))

    two_stop_bot.telegram.reset()
    await two_stop_bot.send(
        driver.tap("staff_failed_reason_501_customer_unavailable", message_id=240)
    )

    refusal = two_stop_bot.telegram.last_shown()
    assert refusal.text == copy_for("staff.delivery.not_found")
    assert refusal.callback_data() == ["staff_active_deliveries"]
    assert len(status_writes(two_stop_bot)) == before, (
        "a reason tapped on a delivery that is no longer the driver's failed an order anyway"
    )


async def test_a_stale_navigate_tap_routes_to_the_stop_whose_button_was_tapped(two_stop_bot):
    """Navigate opens the door of the stop that was tapped — and this one costs
    a real drive across town when it does not.

    ``navigate_to_address`` is registered against ``^staff_navigate_\\d+$``, so
    the delivery id is right there in the tap — and the handler used to never
    read it, building the map link out of whichever ``current_delivery``
    snapshot happened to be loaded. A driver who opened stop B and then tapped
    Navigate on stop A's older card was handed a route to B's door, with A's own
    Back button rewritten to point at B as well. Nothing on the screen said
    which stop it belonged to, so there was no moment where the driver could
    notice.

    The route card renders one message per shift and its stop buttons stay live,
    so "an older card is still on screen" is the normal state of this chat, not
    an edge case. The id is now parsed off ``query.data`` and re-anchored before
    any coordinate is read — the same guard every money handler applies.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    two_stop_bot.telegram.reset()
    await two_stop_bot.send(driver.tap("staff_navigate_501", message_id=240))

    route = two_stop_bot.telegram.last_shown()
    assert "41.2876, 69.2224" in route.text, (
        "the coordinates on screen are not stop 501's own door"
    )
    assert "15 Bunyodkor shoh ko'chasi" in route.text, (
        "stop 502's address is shown under stop 501's button"
    )
    urls = [
        button["url"]
        for row in route.reply_markup["inline_keyboard"]
        for button in row
        if "url" in button
    ]
    assert urls == ["https://yandex.com/maps/?rtext=~41.2876,69.2224&rtt=auto"], (
        "the map link the driver actually follows points at the other customer"
    )
    assert route.callback_data() == ["staff_view_active_501"], (
        "Back leads away from the stop whose button was tapped"
    )


async def test_navigate_on_a_delivery_that_is_gone_refuses_instead_of_routing_anywhere(
    two_stop_bot,
):
    """The stop was reassigned while the driver was looking at the other card.

    Handing them a route built from whatever snapshot is loaded is exactly the
    drive-to-the-wrong-customer failure this guard exists to stop, so say the
    stop is gone and send them back to the list instead.
    """
    driver, labels = await sign_in(two_stop_bot)
    await two_stop_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    await two_stop_bot.send(driver.tap("staff_view_active_501"))
    await two_stop_bot.send(driver.tap("staff_view_active_502"))

    two_stop_bot.desk.active = [row for row in two_stop_bot.desk.active
                                if row["delivery_id"] != 501]

    two_stop_bot.telegram.reset()
    await two_stop_bot.send(driver.tap("staff_navigate_501", message_id=240))

    refusal = two_stop_bot.telegram.last_shown()
    assert refusal.text == copy_for("staff.delivery.not_found")
    assert refusal.callback_data() == ["staff_active_deliveries"]
    assert not any(
        "url" in button
        for row in (refusal.reply_markup or {}).get("inline_keyboard", [])
        for button in row
    ), "a map link was offered for a stop that is no longer the driver's"


# ---------------------------------------------------------------------------
# End of shift
# ---------------------------------------------------------------------------


async def test_the_route_card_says_the_day_is_done_once_the_last_stop_closes(bot):
    """The final screen of the day, and the one that has to offer a next move.

    An empty active list is the normal end state, not an error, and the card
    must say so in words. The "New Orders" button on it is the action a driver
    with no work is actually reaching for — without it they are looking at a
    finished screen with nothing to do but scroll.
    """
    driver, labels = await sign_in(bot)
    bot.desk.active = []
    errors = capture_errors(bot)

    await bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))

    assert not errors, f"rendering an empty route card raised {errors}"
    card = bot.telegram.last_shown()
    assert copy_for("staff.route.all_done") in card.text
    assert card.callback_data() == [
        "staff_route_refresh",
        "staff_new_orders_unified",
        "staff_back_to_main",
    ]


async def test_the_driver_can_tap_the_card_again_and_again_and_it_always_answers(bot):
    """A repeat tap on the route card must always produce a Telegram call.

    The card is edited in place, and an edit whose content is byte-identical is
    rejected by Telegram — so a driver re-tapping "Orders assigned to me" to
    check for new work once made NO API call at all and the bot read as frozen.
    The seconds stamp on a driver-forced render is what fixes that, and this is
    the test that notices if it is ever dropped (or if
    ``staff.route.updated_at`` loses its ``{time}`` placeholder, which silently
    makes every render identical again).
    """
    driver, labels = await sign_in(bot)
    bot.desk.active = [active_row(501, "BS-1001")]
    active_deliveries = menu_label(labels, "staff.menu.active_deliveries")

    await bot.send(driver.text(active_deliveries))
    assert bot.telegram.shown, "the first tap rendered no card"

    for tap in range(2, 5):
        bot.telegram.reset()
        await bot.send(driver.text(active_deliveries))
        assert bot.telegram.of("sendMessage", "editMessageText"), (
            f"tap {tap} on the route card made no Telegram call; the driver sees "
            "a frozen bot and taps harder"
        )
