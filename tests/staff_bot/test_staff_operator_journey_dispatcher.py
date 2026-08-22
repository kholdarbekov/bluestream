"""The operator's whole day, driven through the real staff dispatcher.

An operator is a person on the phone. Somebody calls, wants water, and the
operator has to turn that call into a customer row, an address row and an order
row while the caller waits. Every one of those steps is a separate
``ConversationHandler`` in ``staff_bot/bot.py``, and the operator moves between
them by tapping buttons the bot itself drew on a previous message.

That last sentence is the whole reason this file exists. Handler-level tests
call ``receive_phone(update, context)`` and prove the function is correct; they
cannot prove the operator can ever REACH it. The wiring carries the real risk:

* a keyboard renders ``staff_op_addr_9002`` but no state has that pattern
  registered, so the button is decorative (see the two ratchets at the bottom —
  this is not hypothetical, it is shipped);
* a conversation ends and its confirm button becomes inert, which is correct
  for a double tap and catastrophic if it happens one step early;
* a role guard refuses an entry point but leaves the person parked in a text
  state that then eats their next message;
* the money on screen is quoted from the catalogue (priced for the OPERATOR's
  token) instead of the client's contract, and the operator reads the wrong
  number down the phone.

So every test here starts at ``/start``, reads the buttons off what the bot
actually SENT, and feeds them back through ``Application.process_update``.
Assertions are on two things only: the exact bytes that reached the backend,
and the exact text/buttons the operator would see. Nothing asserts that a mock
was called.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from shared.constants import is_within_tashkent

from staff_bot.handlers.operator.create_user import (
    CONFIRM_CREATE,
    ENTER_FIRST_NAME,
    ENTER_LAST_NAME,
    ENTER_PHONE,
    SELECT_LANGUAGE as CREATE_USER_LANG,
)
from staff_bot.handlers.operator.search_user import SEARCH_INPUT
from staff_bot.handlers.operator.create_order import (
    CONFIRM_ORDER,
    ENTER_NOTES as ORDER_ENTER_NOTES,
    SELECT_ADDRESS,
    SELECT_CLIENT,
    SELECT_PAYMENT,
    SELECT_PRODUCTS,
    SELECT_QUANTITY,
)
from staff_bot.handlers.operator.manage_address import (
    CONFIRM_ADDRESS,
    ENTER_ADDRESS,
    ENTER_DISTRICT,
    ENTER_LABEL,
    ENTER_NOTES as ADDR_ENTER_NOTES,
)

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Real copy, loaded from the seed that ships it
# ---------------------------------------------------------------------------
# Hand-pasting the strings would let a future edit to the seed leave this file
# asserting copy production no longer ships — the test would then be testing
# itself. `_curated_value` is the very function `seed_translations()` calls, so
# what a test asserts is what an operator reads.

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

MENU_KEYS = (
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.create_client",
    "staff.menu.search_client",
    "staff.menu.create_order",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
)

# Every string these tests compare against, verbatim.
COPY_KEYS = (
    "staff.operator.enter_phone",
    "staff.operator.invalid_phone",
    "staff.operator.enter_first_name",
    "staff.operator.invalid_name",
    "staff.operator.enter_last_name",
    "staff.operator.select_client_language",
    "staff.operator.confirm_create_user",
    "staff.operator.user_created",
    "staff.operator.user_exists",
    "staff.operator.user_already_exists",
    "staff.operator.search_prompt",
    "staff.operator.search_too_short",
    "staff.operator.no_results",
    "staff.operator.search_results",
    "staff.operator.order_enter_phone",
    "staff.operator.select_address",
    "staff.operator.select_products",
    "staff.operator.select_quantity",
    "staff.operator.add_more_or_done",
    "staff.operator.select_payment",
    "staff.operator.enter_notes",
    "staff.operator.skip_notes",
    "staff.operator.confirm_order_prompt",
    "staff.operator.confirm_order",
    "staff.operator.order_created",
    "staff.operator.cart",
    "staff.operator.cart_empty",
    "staff.operator.subtotal",
    "staff.operator.done_selecting",
    "staff.operator.no_items_selected",
    "staff.operator.payment_cash",
    "staff.operator.payment_click",
    "staff.operator.payment_unavailable",
    "staff.operator.no_addresses",
    "staff.operator.add_address",
    "staff.operator.create_order_for",
    "staff.operator.manage_addresses",
    "staff.operator.create_user",
    "staff.operator.search_again",
    "staff.operator.enter_address_label",
    "staff.operator.enter_full_address",
    "staff.operator.enter_district",
    "staff.operator.enter_delivery_notes",
    "staff.operator.confirm_address",
    "staff.operator.address_saved",
    "staff.operator.invalid_address",
    "staff.operator.invalid_label",
    "staff.operator.share_location",
    "staff.operator.outside_delivery_area",
    "staff.operator.address_not_found",
    "staff.operator.location_received",
    "staff.operator.location_needs_address",
    "staff.menu.title",
    "staff.cancelled",
    "staff.cancel",
    "staff.confirm",
    "staff.back",
    "staff.unauthorized",
    "staff.session_expired",
    "staff.error_occurred",
    "staff.error.api.validation",
    "staff.error.api.service_unavailable",
    "staff.error.api.conflict",
    "staff.currency.uzs",
    "staff.common.not_available",
    "staff.addresses",
    "staff.orders",
)


def _curated(key: str, language: str) -> str:
    value = _SEED._curated_value(key, language)
    assert value, (
        f"{key} has no curated {language} value in scripts/seed_staff_translations.py — "
        "production would render a humanised placeholder for it"
    )
    return value


def _translation_table(overrides: dict = None) -> dict:
    """The staff translations these tests run against.

    Handed to ``build_staff_harness`` BEFORE ``_setup_handlers`` runs, so this
    is also the table the reply-keyboard menu regexes are compiled from — the
    same coupling production has between a seeded row and the router.
    """
    table = {}
    for key in MENU_KEYS + COPY_KEYS:
        for language in LANGUAGES:
            table[(language, key)] = _curated(key, language)
    table.update(overrides or {})
    return table


# ---------------------------------------------------------------------------
# The world the operator is working in
# ---------------------------------------------------------------------------

LOGIN_ENDPOINT = "/api/v1/staff/auth/login"
OPERATOR_ROOT = "/api/v1/staff/operator"
SEARCH_ENDPOINT = f"{OPERATOR_ROOT}/users/search"
CREATE_USER_ENDPOINT = f"{OPERATOR_ROOT}/users"
CREATE_ORDER_ENDPOINT = f"{OPERATOR_ROOT}/orders"
PRODUCTS_ENDPOINT = "/api/v1/products/"

CLIENT_ID = 4242
CREATED_CLIENT_ID = 7788
EXISTING_ADDRESS_ID = 9001
NEW_ADDRESS_ID = 9002

ADDRESSES_ENDPOINT = f"{OPERATOR_ROOT}/users/{CLIENT_ID}/addresses"
ESTIMATE_ENDPOINT = f"{OPERATOR_ROOT}/users/{CLIENT_ID}/order-estimate"
PAYMENT_METHODS_ENDPOINT = f"{OPERATOR_ROOT}/users/{CLIENT_ID}/payment-methods"

# Shared with the customer bot on purpose — one geocoding surface, not two.
GEOCODE_ENDPOINT = "/api/v1/addresses/geocode"
REVERSE_GEOCODE_ENDPOINT = "/api/v1/addresses/reverse-geocode"

# A point the delivery-zone SSOT accepts, and one it refuses. Both are asserted
# against `is_within_tashkent` where they are used, so a polygon edit cannot
# leave a test quietly proving nothing.
IN_ZONE_PIN = (41.3111, 69.2797)
OUT_OF_ZONE_PIN = (39.6548, 66.9597)  # Samarkand, 280 km away

CLIENT_PHONE = "+998901112233"

BIG_BOTTLE_ID = 11
SMALL_BOTTLE_ID = 12

# What the CATALOGUE says — i.e. what `/api/v1/products/` prices for whoever
# holds the token, and here that is the OPERATOR. Never what a screen may quote.
CATALOGUE = [
    {"id": BIG_BOTTLE_ID, "name": "Suv 19L", "pricing": {"current_price": 45000}},
    {"id": SMALL_BOTTLE_ID, "name": "Suv 5L", "pricing": {"current_price": 15000}},
]

# What THIS client is actually charged (a corporate contract). The gap between
# the two columns is the defect `_display_unit_price` exists to close.
CLIENT_UNIT_PRICE = {BIG_BOTTLE_ID: 27000.0, SMALL_BOTTLE_ID: 9000.0}

CLIENT_ROW = {
    "id": CLIENT_ID,
    "first_name": "Dilnoza",
    "last_name": "Rahimova",
    "phone": CLIENT_PHONE,
    "address_count": 1,
    "order_count": 4,
}

HOME_ADDRESS = {
    "id": EXISTING_ADDRESS_ID,
    "title": "Uy",
    "full_address": "Chilonzor 9-kvartal, 14-uy",
    "district": "Chilonzor",
}


@dataclass
class OperatorWorld:
    """Everything the backend would answer, and everything it recorded."""

    search_result: list = field(default_factory=lambda: [dict(CLIENT_ROW)])
    addresses: list = field(default_factory=lambda: [dict(HOME_ADDRESS)])
    products: list = field(default_factory=lambda: [dict(p) for p in CATALOGUE])
    payment_methods: list = field(
        default_factory=lambda: [
            {"method": "cash", "is_default": True},
            {"method": "click"},
        ]
    )
    payment_restrictions: dict = field(default_factory=dict)
    address_write: object = None  # None => accept; a _StaffFailure => refuse
    order_write: object = None
    # What the backend geocoder answers for a typed address, and what it reads
    # back off a shared pin. Defaults place every address in the city, so a test
    # that is not ABOUT the delivery zone does not have to say so; the zone
    # tests override them.
    geocode: object = field(
        default_factory=lambda: {
            "latitude": IN_ZONE_PIN[0],
            "longitude": IN_ZONE_PIN[1],
            "formatted_address": "Amir Temur ko'chasi 108, Toshkent",
        }
    )
    reverse_geocode: object = field(
        default_factory=lambda: {
            "formatted_address": "Amir Temur ko'chasi 108, Toshkent",
            "district": "Yunusobod",
            "city": "Tashkent",
        }
    )
    created_client: dict = field(
        default_factory=lambda: {"id": CREATED_CLIENT_ID, "phone": CLIENT_PHONE}
    )


def _estimate(call):
    """Price a basket the way ``StaffService.price_phone_order`` would.

    Deliberately derived from the REQUEST, not canned: the screens must state
    the server's numbers for the basket they actually sent, and a canned reply
    would let a bot that sends the wrong quantities still render a right-looking
    total.
    """
    lines = []
    subtotal = 0.0
    for item in (call.data or {}).get("items") or []:
        unit = CLIENT_UNIT_PRICE[item["product_id"]]
        total = unit * item["quantity"]
        subtotal += total
        lines.append(
            {
                "product_id": item["product_id"],
                "product_name": next(
                    p["name"] for p in CATALOGUE if p["id"] == item["product_id"]
                ),
                "quantity": item["quantity"],
                "unit_price": unit,
                "total_price": total,
            }
        )
    return {"items": lines, "subtotal": subtotal}


def wire_backend(harness, world: OperatorWorld):
    """Point every operator endpoint the flows use at ``world``."""
    backend = harness.backend

    backend.route("GET", SEARCH_ENDPOINT, lambda _c: {"items": world.search_result})
    backend.route(
        "POST",
        CREATE_USER_ENDPOINT,
        lambda _c: world.created_client,
    )
    backend.route("GET", ADDRESSES_ENDPOINT, lambda _c: {"items": world.addresses})
    backend.route("GET", PRODUCTS_ENDPOINT, lambda _c: {"items": world.products})
    backend.route("POST", GEOCODE_ENDPOINT, lambda _c: world.geocode)
    backend.route("POST", REVERSE_GEOCODE_ENDPOINT, lambda _c: world.reverse_geocode)
    backend.route("POST", ESTIMATE_ENDPOINT, _estimate)
    backend.route(
        "GET",
        PAYMENT_METHODS_ENDPOINT,
        lambda _c: {
            "available_methods": world.payment_methods,
            "payment_restrictions": world.payment_restrictions,
        },
    )

    def _write_address(call):
        if world.address_write is not None:
            return world.address_write
        saved = {"id": NEW_ADDRESS_ID, **(call.data or {})}
        world.addresses.append(saved)
        return saved

    backend.route("POST", ADDRESSES_ENDPOINT, _write_address)

    def _write_order(call):
        if world.order_write is not None:
            return world.order_write
        return {"id": 5150, "order_number": "BS-260821-0042"}

    backend.route("POST", CREATE_ORDER_ENDPOINT, _write_order)
    return world


# ---------------------------------------------------------------------------
# A signed-in staff member
# ---------------------------------------------------------------------------


def _staff_row(roles, language):
    return {
        "id": 55,
        "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
        "first_name": "Aziz",
        "last_name": "Karimov",
        "phone": "+998901112233",
        "preferred_language": language,
        "role": "operator",
        "status": "active",
        "staff_roles": json.dumps(roles),
        "staff_bot_state": "{}",
    }


def _login_payload(roles, language):
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
            "staff_roles": roles,
        },
    }


async def build_staff(monkeypatch, *, roles, language="en", translations=None):
    harness = await build_staff_harness(
        monkeypatch,
        translations=translations if translations is not None else _translation_table(),
        database=FakeStaffDatabase(staff_user=_staff_row(roles, language)),
    )
    harness.backend.route("POST", LOGIN_ENDPOINT, lambda _c: _login_payload(roles, language))
    return harness


async def sign_in(harness):
    """Run the real ``/start`` login; return (update factory, menu labels)."""
    staff_member = harness.updates()
    await harness.send(staff_member.command("start"))

    shown = harness.telegram.shown
    assert shown, "/start produced no message at all — the operator sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    harness.telegram.reset()
    harness.backend.calls.clear()
    return staff_member, labels


# ---------------------------------------------------------------------------
# Reading the screen
# ---------------------------------------------------------------------------


def menu_label(labels, key, language="en") -> str:
    """The one rendered button carrying ``key``'s translation.

    Matched on the translated VALUE, not on a rebuilt ``f"{emoji} {value}"``, so
    the emoji stays an implementation detail of the keyboard.
    """
    value = _curated(key, language)
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, f"expected exactly one menu button carrying {value!r}, got {hits}"
    return hits[0]


def texts(harness) -> list:
    return [call.text for call in harness.telegram.shown]


def last_screen(harness):
    return harness.telegram.last_shown()


def alerts(harness) -> list:
    """Every toast/alert the operator would have seen pop up."""
    return [
        call.params.get("text", "")
        for call in harness.telegram.of("answerCallbackQuery")
        if call.params.get("text")
    ]


def backend_calls(harness, method, endpoint) -> list:
    return [c for c in harness.backend.calls if c.method == method and c.endpoint == endpoint]


def user_data(harness) -> dict:
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


def capture_errors(harness) -> list:
    """Collect every exception PTB would otherwise log and swallow.

    Without this a handler that raises is indistinguishable from one that
    quietly did nothing — and the operator sees the same thing either way.
    """
    errors = []

    async def _record(_update, context):
        errors.append(context.error)

    harness.application.add_error_handler(_record)
    return errors


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def operator(monkeypatch):
    harness = await build_staff(monkeypatch, roles=["operator"], language="en")
    harness.world = wire_backend(harness, OperatorWorld())
    harness.errors = capture_errors(harness)
    return harness


@pytest.fixture
async def driver(monkeypatch):
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language="en")
    harness.world = wire_backend(harness, OperatorWorld())
    harness.errors = capture_errors(harness)
    return harness


# ---------------------------------------------------------------------------
# Journey 1 — a new customer phones in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_language", ["uz", "ru", "en"])
async def test_an_operator_creates_a_client_and_the_backend_gets_exactly_what_was_typed(
    operator, client_language
):
    """The whole create-a-customer call, tap by tap, in one go.

    Everything downstream of this — which language the customer's order
    notifications arrive in, whether their phone can ever be found again by
    search — is decided by the four fields this flow POSTs. So the assertion is
    the payload itself, not "a POST happened". Each of the three language
    buttons is exercised because ``select_client_language`` parses the code out
    of the callback data by splitting on ``_``; a renamed button would silently
    write a garbage language code that no lookup ever matches.

    The typed phone is deliberately the messy national form a person reads off
    a screen. If normalisation regresses, the customer is stored under a phone
    the search and the customer bot's own login will never match, and they
    become unreachable.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []  # nobody with this phone yet

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    assert operator.conversation_state("staff_create_user") == ENTER_PHONE
    assert texts(operator)[-1] == _curated("staff.operator.enter_phone", "en")

    await operator.send(ops.text(" 90 111 22 33 "))
    assert operator.conversation_state("staff_create_user") == ENTER_FIRST_NAME
    assert texts(operator)[-1] == _curated("staff.operator.enter_first_name", "en")

    await operator.send(ops.text("Dilnoza"))
    assert operator.conversation_state("staff_create_user") == ENTER_LAST_NAME

    await operator.send(ops.text("Rahimova"))
    assert operator.conversation_state("staff_create_user") == CREATE_USER_LANG
    language_buttons = last_screen(operator).callback_data()
    assert language_buttons == ["staff_op_lang_uz", "staff_op_lang_ru", "staff_op_lang_en"]

    await operator.send(ops.tap(f"staff_op_lang_{client_language}"))
    assert operator.conversation_state("staff_create_user") == CONFIRM_CREATE

    confirm_screen = last_screen(operator)
    assert _curated("staff.operator.confirm_create_user", "en") in confirm_screen.text
    assert "+998901112233" in confirm_screen.text, (
        "the operator confirms against what they typed; showing an unnormalised "
        "phone here hides exactly the mistake this screen exists to catch"
    )
    assert "Dilnoza Rahimova" in confirm_screen.text
    assert confirm_screen.callback_data() == [
        "staff_op_confirm_create_user",
        "staff_back_to_main",
    ]

    assert backend_calls(operator, "POST", CREATE_USER_ENDPOINT) == [], (
        "nothing may be written before the operator confirms"
    )

    await operator.send(ops.tap("staff_op_confirm_create_user"))

    writes = backend_calls(operator, "POST", CREATE_USER_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data == {
        "phone": "+998901112233",
        "first_name": "Dilnoza",
        "last_name": "Rahimova",
        "preferred_language": client_language,
    }

    assert operator.conversation_state("staff_create_user") is None
    assert "new_client" not in user_data(operator), (
        "half-entered client data left behind leaks into the operator's NEXT call"
    )

    done = last_screen(operator)
    assert _curated("staff.operator.user_created", "en") in done.text
    assert "Dilnoza Rahimova" in done.text and "+998901112233" in done.text
    assert done.callback_data() == [
        f"staff_op_order_{CREATED_CLIENT_ID}",
        f"staff_op_addresses_{CREATED_CLIENT_ID}",
        "staff_back_to_main",
    ], "the operator must be able to go straight from 'created' to 'order for them'"
    assert operator.errors == []


async def test_a_phone_the_bot_cannot_normalise_is_refused_without_touching_the_backend(operator):
    """The caller reads their number out wrong, which happens constantly.

    Two things must hold. The operator stays on the phone prompt — bouncing
    them to the menu means starting the call over with a customer already
    impatient. And nothing reaches the backend: a lookup on a junk string is a
    wasted round trip while somebody is on the line, and creating a user from
    it is unrecoverable.
    """
    ops, labels = await sign_in(operator)
    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    operator.telegram.reset()

    for junk in ("12345", "not a phone", "+1 202 555 0143"):
        await operator.send(ops.text(junk))
        assert operator.conversation_state("staff_create_user") == ENTER_PHONE, (
            f"{junk!r} knocked the operator out of the phone prompt"
        )
        assert texts(operator)[-1] == _curated("staff.operator.invalid_phone", "en")

    assert operator.backend.calls == [], (
        f"a rejected phone still reached the backend: {operator.backend.calls}"
    )

    # And the flow is not poisoned — the corrected number carries straight on.
    operator.world.search_result = []
    await operator.send(ops.text("+998901112233"))
    assert operator.conversation_state("staff_create_user") == ENTER_FIRST_NAME


async def test_a_one_letter_name_is_refused_and_the_phone_already_typed_survives(operator):
    """A slip on the keyboard mid-form must not cost the operator the form.

    ``validate_name`` rejects a single character. The retry has to land back on
    the SAME step with the phone still captured; re-asking for the phone is how
    an operator ends up reading the number back to the caller a third time, and
    is the moment they give up on the bot and use the admin panel instead.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await operator.send(ops.text("+998901112233"))
    operator.telegram.reset()

    await operator.send(ops.text("D"))
    assert operator.conversation_state("staff_create_user") == ENTER_FIRST_NAME
    assert texts(operator)[-1] == _curated("staff.operator.invalid_name", "en")
    assert user_data(operator)["new_client"] == {"phone": "+998901112233"}, (
        "the already-captured phone was dropped when the name was rejected"
    )

    await operator.send(ops.text("Dilnoza"))
    assert operator.conversation_state("staff_create_user") == ENTER_LAST_NAME
    assert user_data(operator)["new_client"]["phone"] == "+998901112233"


async def test_creating_a_client_who_already_has_an_account_stops_before_writing_anything(operator):
    """Duplicate customers are how a household's bottle balance splits in two.

    The flow looks the phone up before it asks for a name, and on an exact
    match it must STOP — show their card, offer to order for the account that
    exists, and write nothing. If the guard regresses the operator keeps typing
    and creates a second row for the same person, whose bottle debt and loyalty
    then live on the wrong account.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = [dict(CLIENT_ROW)]

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    operator.telegram.reset()
    await operator.send(ops.text(CLIENT_PHONE))

    lookups = backend_calls(operator, "GET", SEARCH_ENDPOINT)
    assert len(lookups) == 1
    assert lookups[0].params == {"q": CLIENT_PHONE, "type": "phone"}

    assert backend_calls(operator, "POST", CREATE_USER_ENDPOINT) == []
    assert operator.conversation_state("staff_create_user") is None, (
        "the flow must END on a duplicate, not sit waiting for a first name"
    )

    screen = last_screen(operator)
    assert _curated("staff.operator.user_exists", "en") in screen.text
    assert "Dilnoza Rahimova" in screen.text and CLIENT_PHONE in screen.text
    assert screen.callback_data() == [
        f"staff_op_order_{CLIENT_ID}",
        f"staff_op_addresses_{CLIENT_ID}",
        "staff_back_to_main",
    ]


async def test_an_operator_who_backs_out_at_the_confirm_screen_creates_nobody(operator):
    """The give-up path. The caller changes their mind at the last second.

    Cancel must do three things: write nothing, clear the half-built client out
    of ``user_data``, and land the operator back on a WORKING main menu. The
    last one is a two-group affair — the conversation's own fallback says
    "Cancelled" in group 0 and the global back handler re-renders the menu in
    group 1 — so a change to either group can leave the operator staring at a
    dead-end "Cancelled" with nowhere to go while the caller waits.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await operator.send(ops.text("+998901112233"))
    await operator.send(ops.text("Dilnoza"))
    await operator.send(ops.text("-"))  # skip last name
    await operator.send(ops.tap("staff_op_lang_uz"))
    assert operator.conversation_state("staff_create_user") == CONFIRM_CREATE
    operator.telegram.reset()

    await operator.send(ops.tap("staff_back_to_main"))

    assert backend_calls(operator, "POST", CREATE_USER_ENDPOINT) == []
    assert operator.conversation_state("staff_create_user") is None
    assert "new_client" not in user_data(operator)
    assert _curated("staff.cancelled", "en") in texts(operator)

    menu = last_screen(operator)
    assert menu.text == _curated("staff.menu.title", "en")
    assert "staff_create_client" in menu.callback_data(), (
        "backing out dropped the operator somewhere they cannot start again from"
    )


async def test_confirming_client_creation_twice_still_creates_exactly_one_client(operator):
    """Phones lag; operators tap Confirm again.

    One tap must equal one customer. The second tap arrives after the
    conversation has ended, so no handler claims it — which is the right
    OUTCOME (nothing is written) reached by a slightly unfriendly route: the
    operator gets no acknowledgement at all and Telegram leaves the button
    spinning. That silence is a UX wart worth fixing one day; a duplicate
    customer row is a data problem nobody can unwind, so this pins the row.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await operator.send(ops.text("+998901112233"))
    await operator.send(ops.text("Dilnoza"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_lang_uz"))

    await operator.send(ops.tap("staff_op_confirm_create_user"))
    await operator.send(ops.tap("staff_op_confirm_create_user"))

    assert len(backend_calls(operator, "POST", CREATE_USER_ENDPOINT)) == 1, (
        "a double tap created the customer twice"
    )
    assert operator.errors == []


async def test_a_russian_speaking_operator_never_sees_an_english_prompt(monkeypatch):
    """The fleet runs on three languages and English is nobody's first choice.

    This bot has shipped English leaks three separate ways (a key outside the
    ``staff.`` namespace, a hand-written dynamic family, a raw backend enum), and
    every one of them looked fine in an English test. So walk the create-client
    flow in Russian and compare each prompt to the Russian row the seed actually
    ships. A missing row does not crash — it renders a humanised English-ish
    stub — which is exactly why only a verbatim comparison catches it.
    """
    harness = await build_staff(monkeypatch, roles=["operator"], language="ru")
    wire_backend(harness, OperatorWorld(search_result=[]))
    errors = capture_errors(harness)
    ops, labels = await sign_in(harness)

    await harness.send(ops.text(menu_label(labels, "staff.menu.create_client", "ru")))
    assert texts(harness)[-1] == _curated("staff.operator.enter_phone", "ru")

    await harness.send(ops.text("nope"))
    assert texts(harness)[-1] == _curated("staff.operator.invalid_phone", "ru")

    await harness.send(ops.text("+998901112233"))
    assert texts(harness)[-1] == _curated("staff.operator.enter_first_name", "ru")

    await harness.send(ops.text("Dilnoza"))
    assert texts(harness)[-1] == _curated("staff.operator.enter_last_name", "ru")

    await harness.send(ops.text("-"))
    assert texts(harness)[-1] == _curated("staff.operator.select_client_language", "ru")

    await harness.send(ops.tap("staff_op_lang_ru"))
    assert _curated("staff.operator.confirm_create_user", "ru") in texts(harness)[-1]
    assert last_screen(harness).button_labels() == [
        f"✅ {_curated('staff.confirm', 'ru')}",
        f"❌ {_curated('staff.cancel', 'ru')}",
    ]

    await harness.send(ops.tap("staff_op_confirm_create_user"))
    assert _curated("staff.operator.user_created", "ru") in texts(harness)[-1]
    assert errors == []


# ---------------------------------------------------------------------------
# Journey 2 — finding somebody who already exists
# ---------------------------------------------------------------------------


async def test_searching_by_phone_asks_for_a_phone_search_and_shows_that_one_client(operator):
    """Search is the operator's most-used screen and its query is typed by hand.

    ``detect_search_type`` decides between a phone lookup and a name lookup,
    and the backend runs a completely different query for each. Sending
    ``type=name`` for a phone means digits are matched against names and the
    caller is told they have no account — while they are on the line insisting
    they order every week.
    """
    ops, labels = await sign_in(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    assert operator.conversation_state("staff_search_user") == SEARCH_INPUT
    assert texts(operator)[-1] == _curated("staff.operator.search_prompt", "en")

    operator.telegram.reset()
    await operator.send(ops.text("90 111 22 33"))

    queries = backend_calls(operator, "GET", SEARCH_ENDPOINT)
    assert len(queries) == 1
    assert queries[0].params == {"q": "90 111 22 33", "type": "phone"}

    card = last_screen(operator)
    assert "Dilnoza Rahimova" in card.text and CLIENT_PHONE in card.text
    assert card.callback_data() == [
        f"staff_op_order_{CLIENT_ID}",
        f"staff_op_addresses_{CLIENT_ID}",
        "staff_back_to_main",
    ]
    assert operator.conversation_state("staff_search_user") is None, (
        "a finished search must not stay armed, or the operator's next message "
        "is swallowed as another search query"
    )


async def test_searching_by_name_gives_every_match_its_own_order_button(operator):
    """Three sisters at one address, all called Rahimova.

    The operator has to pick the right one, so each result needs its OWN
    actionable card carrying its OWN id. A shared keyboard, or a header with no
    per-row buttons, means the order is created against whichever row the code
    happened to keep — and the water is billed to the wrong sister.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = [
        {**CLIENT_ROW, "id": 4242, "first_name": "Dilnoza"},
        {**CLIENT_ROW, "id": 4343, "first_name": "Nilufar"},
        {**CLIENT_ROW, "id": 4444, "first_name": "Zilola"},
    ]

    await operator.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    operator.telegram.reset()
    await operator.send(ops.text("Rahimova"))

    queries = backend_calls(operator, "GET", SEARCH_ENDPOINT)
    assert queries[0].params == {"q": "Rahimova", "type": "name"}

    shown = operator.telegram.shown
    assert shown[0].text == _curated("staff.operator.search_results", "en").format(count=3)

    order_buttons = [call.callback_data()[0] for call in shown[1:]]
    assert order_buttons == [
        "staff_op_order_4242",
        "staff_op_order_4343",
        "staff_op_order_4444",
    ]
    names = [call.text.split("\n")[0] for call in shown[1:]]
    assert all(
        name in " ".join(names) for name in ("Dilnoza", "Nilufar", "Zilola")
    ), f"a result was rendered without its own name: {names}"


async def test_a_search_that_finds_nobody_offers_to_create_them_and_that_button_works(operator):
    """The handoff between two separate ConversationHandlers.

    "Not found" is the most common search outcome for a first-time caller, and
    the only sane next step is to create them. That button lives on a
    ``search_user`` screen but is an ENTRY POINT of ``create_user`` — a wiring
    seam no handler-level test can see. If it is not registered, the operator
    taps a live-looking button and nothing happens at all.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    operator.telegram.reset()
    await operator.send(ops.text("Rahimova"))

    screen = last_screen(operator)
    assert screen.text == _curated("staff.operator.no_results", "en").format(query="Rahimova")
    assert screen.callback_data() == [
        "staff_op_create_user",
        "staff_search_client",
        "staff_back_to_main",
    ]
    assert operator.conversation_state("staff_search_user") is None

    await operator.send(ops.tap("staff_op_create_user"))

    assert operator.conversation_state("staff_create_user") == ENTER_PHONE, (
        "'create this client' rendered on the not-found screen but opens nothing"
    )
    assert texts(operator)[-1] == _curated("staff.operator.enter_phone", "en")


async def test_a_one_character_search_never_reaches_the_backend(operator):
    """A stray keystroke, or the operator's phone auto-sending.

    A one-character query matches half the customer base, so the guard is in
    the bot. It must keep the operator on the prompt AND spend no round trip:
    this endpoint runs an ILIKE across names and phones, and an accidental
    single letter is the cheapest way to make it scan everything.
    """
    ops, labels = await sign_in(operator)
    await operator.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    operator.telegram.reset()
    operator.backend.calls.clear()

    await operator.send(ops.text("R"))

    assert operator.backend.calls == []
    assert operator.conversation_state("staff_search_user") == SEARCH_INPUT
    assert texts(operator)[-1] == _curated("staff.operator.search_too_short", "en")


async def test_a_backend_outage_during_search_keeps_the_operator_on_the_prompt(operator):
    """The backend falls over with a caller on the line.

    The operator must be told, and must stay where they are so a retry is one
    message away. Ending the conversation here would drop them to the menu and
    make them re-navigate mid-call; saying nothing would make them retype the
    name into a bot they now believe is broken.
    """
    ops, labels = await sign_in(operator)
    await operator.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    operator.telegram.reset()

    operator.backend.route(
        "GET", SEARCH_ENDPOINT, lambda _c: staff_backend_failure("boom", status_code=500)
    )
    await operator.send(ops.text("Rahimova"))

    assert texts(operator)[-1] == f"❌ {_curated('staff.error.api.service_unavailable', 'en')}"
    assert operator.conversation_state("staff_search_user") == SEARCH_INPUT

    # Backend recovers; the same typed query now works, no re-navigation needed.
    operator.backend.route("GET", SEARCH_ENDPOINT, lambda _c: {"items": [dict(CLIENT_ROW)]})
    await operator.send(ops.text("Rahimova"))
    assert "Dilnoza Rahimova" in last_screen(operator).text


# ---------------------------------------------------------------------------
# Journey 3 — taking the order
# ---------------------------------------------------------------------------


async def test_an_operator_takes_an_order_end_to_end_and_the_backend_gets_the_chosen_basket(
    operator,
):
    """The full phone order: find the caller, pick where, pick what, pick how
    they pay, add a note, confirm.

    Seven screens and six conversation states, each one reached only by a
    callback pattern registered in the PREVIOUS state. This is the test that
    notices when a keyboard and its state stop agreeing.

    The money assertions are the sharp end. Every figure on screen must come
    from the server's client-scoped quote, because ``/api/v1/products/`` prices
    for whoever holds the token — the OPERATOR. Quoting 45 000 down the phone
    for an order the backend charges 27 000 for is a shipped defect this file
    refuses to let back in.
    """
    ops, labels = await sign_in(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    assert operator.conversation_state("staff_create_order") == SELECT_CLIENT
    assert texts(operator)[-1] == _curated("staff.operator.order_enter_phone", "en")

    await operator.send(ops.text(CLIENT_PHONE))
    card = last_screen(operator)
    assert card.callback_data()[0] == f"staff_op_order_{CLIENT_ID}"

    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    assert operator.conversation_state("staff_create_order") == SELECT_ADDRESS
    picker = last_screen(operator)
    assert picker.text == _curated("staff.operator.select_address", "en")
    assert picker.callback_data() == [
        f"staff_op_addr_{EXISTING_ADDRESS_ID}",
        f"staff_op_add_addr_{CLIENT_ID}",
        "staff_back_to_main",
    ]
    assert "Uy" in " ".join(picker.button_labels())

    await operator.send(ops.tap(f"staff_op_addr_{EXISTING_ADDRESS_ID}"))
    assert operator.conversation_state("staff_create_order") == SELECT_PRODUCTS
    catalogue_screen = last_screen(operator)
    assert _curated("staff.operator.cart_empty", "en") in catalogue_screen.text
    assert catalogue_screen.button_labels()[:2] == [
        f"Suv 19L - 27,000 {_curated('staff.currency.uzs', 'en')}",
        f"Suv 5L - 9,000 {_curated('staff.currency.uzs', 'en')}",
    ], (
        "the product buttons quoted the catalogue price instead of this client's "
        "contract price — the operator would read the wrong number down the phone"
    )

    await operator.send(ops.tap(f"staff_op_product_{BIG_BOTTLE_ID}"))
    assert operator.conversation_state("staff_create_order") == SELECT_QUANTITY
    quantity_screen = last_screen(operator)
    assert f"27,000 {_curated('staff.currency.uzs', 'en')}" in quantity_screen.text
    assert f"staff_op_qty_{BIG_BOTTLE_ID}_3" in quantity_screen.callback_data()

    await operator.send(ops.tap(f"staff_op_qty_{BIG_BOTTLE_ID}_3"))
    assert operator.conversation_state("staff_create_order") == SELECT_PRODUCTS
    cart_screen = last_screen(operator)
    uzs = _curated("staff.currency.uzs", "en")
    assert f"Suv 19L x3 — 81,000 {uzs}" in cart_screen.text
    assert f"{_curated('staff.operator.subtotal', 'en')}: 81,000 {uzs}" in cart_screen.text

    await operator.send(ops.tap("staff_op_products_done"))
    assert operator.conversation_state("staff_create_order") == SELECT_PAYMENT
    method_calls = backend_calls(operator, "GET", PAYMENT_METHODS_ENDPOINT)
    assert len(method_calls) == 1
    assert method_calls[0].params == {"delivery_address_id": EXISTING_ADDRESS_ID}, (
        "without the destination the backend cannot evaluate the COD cap's PLACE arm, "
        "and a coworker's debt gets relayed to the caller as their own"
    )
    payment_screen = last_screen(operator)
    assert payment_screen.callback_data() == [
        "staff_op_pay_cash",
        "staff_op_pay_click",
        "staff_back_to_main",
    ]

    await operator.send(ops.tap("staff_op_pay_cash"))
    assert operator.conversation_state("staff_create_order") == ORDER_ENTER_NOTES
    assert texts(operator)[-1] == _curated("staff.operator.enter_notes", "en")

    await operator.send(ops.text("Eshik oldiga qoldiring"))
    assert operator.conversation_state("staff_create_order") == CONFIRM_ORDER
    summary = last_screen(operator)
    assert _curated("staff.operator.payment_cash", "en") in summary.text
    assert "Eshik oldiga qoldiring" in summary.text
    assert f"{_curated('staff.operator.subtotal', 'en')}: 81,000 {uzs}" in summary.text
    assert summary.callback_data() == ["staff_op_confirm_order", "staff_back_to_main"]

    assert backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT) == []

    await operator.send(ops.tap("staff_op_confirm_order"))

    writes = backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data == {
        "client_id": CLIENT_ID,
        "items": [{"product_id": BIG_BOTTLE_ID, "quantity": 3}],
        "delivery_address_id": EXISTING_ADDRESS_ID,
        "payment_method": "cash",
        "delivery_notes": "Eshik oldiga qoldiring",
    }

    done = last_screen(operator)
    assert done.text == "✅ " + _curated("staff.operator.order_created", "en").format(
        order_number="BS-260821-0042"
    )
    assert operator.conversation_state("staff_create_order") is None
    assert "new_order" not in user_data(operator)
    assert "available_products" not in user_data(operator)
    assert operator.errors == []


async def test_a_payment_method_the_backend_did_not_offer_is_refused_from_a_stale_keyboard(
    operator,
):
    """The operator scrolls up and taps Cash on an older order's message.

    Payment availability is decided by the backend (COD caps, contract rails)
    and re-sent on every basket. An inline keyboard from three minutes ago is
    still tappable forever, so the bot re-checks the tapped method against the
    list the backend just published. Without that check the operator books a
    cash order for a customer the COD cap blocks, the driver turns up, and the
    money cannot be taken at the door.
    """
    ops, labels = await sign_in(operator)
    operator.world.payment_methods = [{"method": "click", "is_default": True}]

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_addr_{EXISTING_ADDRESS_ID}"))
    await operator.send(ops.tap(f"staff_op_product_{BIG_BOTTLE_ID}"))
    await operator.send(ops.tap(f"staff_op_qty_{BIG_BOTTLE_ID}_1"))
    await operator.send(ops.tap("staff_op_products_done"))
    assert operator.conversation_state("staff_create_order") == SELECT_PAYMENT
    assert last_screen(operator).callback_data() == ["staff_op_pay_click", "staff_back_to_main"]

    operator.telegram.reset()
    await operator.send(ops.tap("staff_op_pay_cash"))

    assert _curated("staff.operator.payment_unavailable", "en") in alerts(operator), (
        f"the refusal was silent; the operator sees only alerts: {alerts(operator)}"
    )
    assert operator.conversation_state("staff_create_order") == SELECT_PAYMENT, (
        "a refused method must leave the operator on the payment screen"
    )
    assert user_data(operator)["new_order"]["payment_method"] == "click", (
        "the refused method was written onto the order anyway"
    )

    # The offered method still works, so the call is not a dead end.
    await operator.send(ops.tap("staff_op_pay_click"))
    assert operator.conversation_state("staff_create_order") == ORDER_ENTER_NOTES
    await operator.send(ops.tap("staff_op_skip_notes"))
    await operator.send(ops.tap("staff_op_confirm_order"))
    assert backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)[0].data["payment_method"] == (
        "click"
    )
    assert backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)[0].data["delivery_notes"] is None


async def test_an_order_the_backend_rejects_leaves_the_operator_on_the_confirm_screen(operator):
    """The backend refuses at the last step — stock ran out, the COD cap moved.

    The operator must keep the basket. Dropping them to the menu means
    rebuilding seven screens of choices with the caller still waiting, so the
    contract is: show the refusal, stay in ``CONFIRM_ORDER``, and let the same
    button work the moment the backend recovers.
    """
    ops, labels = await sign_in(operator)
    operator.world.order_write = staff_backend_failure(
        "conflict", status_code=409, error_code="STAFF_MAX_CONCURRENT_REACHED"
    )

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_addr_{EXISTING_ADDRESS_ID}"))
    await operator.send(ops.tap(f"staff_op_product_{SMALL_BOTTLE_ID}"))
    await operator.send(ops.tap(f"staff_op_qty_{SMALL_BOTTLE_ID}_2"))
    await operator.send(ops.tap("staff_op_products_done"))
    await operator.send(ops.tap("staff_op_pay_cash"))
    await operator.send(ops.tap("staff_op_skip_notes"))
    assert operator.conversation_state("staff_create_order") == CONFIRM_ORDER

    operator.telegram.reset()
    await operator.send(ops.tap("staff_op_confirm_order"))

    assert f"❌ {_curated('staff.error.api.conflict', 'en')}" in alerts(operator)
    assert operator.conversation_state("staff_create_order") == CONFIRM_ORDER
    assert user_data(operator)["new_order"]["items"] == [
        {
            "product_id": SMALL_BOTTLE_ID,
            "quantity": 2,
            "name": "Suv 5L",
            "price": 9000.0,
        }
    ], "the basket was thrown away when the backend refused"

    operator.world.order_write = None
    await operator.send(ops.tap("staff_op_confirm_order"))

    writes = backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)
    assert len(writes) == 2, "the retry never reached the backend"
    assert writes[1].data["items"] == [{"product_id": SMALL_BOTTLE_ID, "quantity": 2}]
    assert operator.conversation_state("staff_create_order") is None


async def test_abandoning_the_order_at_the_product_step_throws_the_basket_away(operator):
    """The caller hangs up halfway through choosing products.

    Cancel has to leave nothing behind. ``available_products`` in particular is
    a whole catalogue snapshot keyed by id; if it survives into the NEXT call,
    ``select_quantity`` will happily price a product against the previous
    client's contract quote.
    """
    ops, labels = await sign_in(operator)

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_addr_{EXISTING_ADDRESS_ID}"))
    await operator.send(ops.tap(f"staff_op_product_{BIG_BOTTLE_ID}"))
    await operator.send(ops.tap(f"staff_op_qty_{BIG_BOTTLE_ID}_5"))
    assert user_data(operator)["new_order"]["items"]
    operator.telegram.reset()

    await operator.send(ops.tap("staff_back_to_main"))

    assert backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT) == []
    assert operator.conversation_state("staff_create_order") is None
    assert "new_order" not in user_data(operator)
    assert "available_products" not in user_data(operator)
    assert _curated("staff.cancelled", "en") in texts(operator)

    menu = last_screen(operator)
    assert menu.text == _curated("staff.menu.title", "en")
    assert "staff_create_order" in menu.callback_data(), (
        "the operator must be able to start the next order straight from where cancel left them"
    )


# ---------------------------------------------------------------------------
# Journey 4 — where do we deliver it?
# ---------------------------------------------------------------------------


async def test_adding_the_first_address_for_a_client_sends_the_fields_under_the_backend_keys(
    operator,
):
    """A brand-new customer has nowhere to deliver to yet.

    Four typed fields go to ``StaffService.add_client_address``, which reads
    ``title`` / ``full_address`` / ``district`` / ``delivery_notes`` — and
    silently defaults ``title`` to "Home" for anything it does not recognise.
    So the payload keys ARE the feature: a label stored under the wrong key
    reads back as "Home" for every address the operator ever creates, and the
    driver cannot tell the office from the flat.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))

    empty_screen = last_screen(operator)
    assert empty_screen.text == _curated("staff.operator.no_addresses", "en")
    assert empty_screen.callback_data() == [
        f"staff_op_add_addr_{CLIENT_ID}",
        "staff_back_to_main",
    ]

    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    assert operator.conversation_state("staff_add_address") == ENTER_LABEL
    assert texts(operator)[-1] == _curated("staff.operator.enter_address_label", "en")

    await operator.send(ops.text("Ofis"))
    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS

    await operator.send(ops.text("Amir Temur ko'chasi 108, 3-qavat"))
    assert operator.conversation_state("staff_add_address") == ENTER_DISTRICT

    await operator.send(ops.text("Yunusobod"))
    assert operator.conversation_state("staff_add_address") == ADDR_ENTER_NOTES

    await operator.send(ops.text("Qo'ng'iroq ishlamaydi"))
    assert operator.conversation_state("staff_add_address") == CONFIRM_ADDRESS
    confirm = last_screen(operator)
    assert _curated("staff.operator.confirm_address", "en") in confirm.text
    for typed in (
        "Ofis",
        "Amir Temur ko'chasi 108, 3-qavat",
        "Yunusobod",
        "Qo'ng'iroq ishlamaydi",
    ):
        assert typed in confirm.text, (
            f"{typed!r} never made it onto the confirmation screen, so the operator "
            "is confirming something they cannot check"
        )
    assert confirm.callback_data() == ["staff_op_confirm_address", "staff_back_to_main"]

    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT) == []

    await operator.send(ops.tap("staff_op_confirm_address"))

    writes = backend_calls(operator, "POST", ADDRESSES_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data == {
        "title": "Ofis",
        "full_address": "Amir Temur ko'chasi 108, 3-qavat",
        "district": "Yunusobod",
        "delivery_notes": "Qo'ng'iroq ishlamaydi",
        # The pin the typed line geocoded to. It ships with the four typed
        # fields because `ensure_within_delivery_zone` — which the backend runs
        # on this very payload — is a NO-OP without it.
        "latitude": IN_ZONE_PIN[0],
        "longitude": IN_ZONE_PIN[1],
    }
    assert "label" not in writes[0].data, (
        "a 'label' key is ignored by the backend, which then defaults the title to 'Home'"
    )

    assert operator.conversation_state("staff_add_address") is None
    assert "new_address" not in user_data(operator)
    assert "adding_address_for" not in user_data(operator)


async def test_a_too_short_address_is_refused_and_the_label_already_typed_survives(operator):
    """Operators hit Enter early; a two-character address is a slip, not a place.

    The retry must land on the same step with the label intact — and, more
    importantly, nothing may be POSTed. An address row with a two-character
    ``full_address`` is invisible garbage that a driver only discovers standing
    on a street with a crate of water.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))
    operator.telegram.reset()

    await operator.send(ops.text("A1"))

    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS
    assert texts(operator)[-1] == _curated("staff.operator.invalid_address", "en")
    assert user_data(operator)["new_address"] == {"title": "Uy"}
    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT) == []


async def test_an_address_the_backend_refuses_as_out_of_zone_is_never_reported_as_saved(operator):
    """A caller in Samarkand. The delivery polygon says no.

    ``shared.constants.TASHKENT_POLYGON`` is the SSOT and
    ``StaffService.add_client_address`` enforces it through
    ``ensure_within_delivery_zone`` before anything is written, so the refusal
    arrives here as a 400. What must NOT happen is the bot claiming success
    anyway: the operator would tell the caller their address is on file, and
    the next screen would offer an address picker for a row that does not
    exist.
    """
    samarkand = (39.6548, 66.9597)
    assert not is_within_tashkent(*samarkand), (
        "the polygon moved — pick a point the delivery-zone SSOT really rejects"
    )
    assert is_within_tashkent(41.2995, 69.2401), "control: the city centre must be inside"

    ops, labels = await sign_in(operator)
    operator.world.addresses = []
    operator.world.address_write = staff_backend_failure(
        "The selected location is outside our delivery area (Tashkent).", status_code=400
    )

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))
    await operator.send(ops.text("Samarqand, Registon ko'chasi 5"))
    await operator.send(ops.text("-"))
    await operator.send(ops.text("-"))
    assert operator.conversation_state("staff_add_address") == CONFIRM_ADDRESS

    operator.telegram.reset()
    await operator.send(ops.tap("staff_op_confirm_address"))

    assert f"❌ {_curated('staff.error.api.validation', 'en')}" in alerts(operator), (
        f"the operator was not told the address was refused; alerts: {alerts(operator)}"
    )
    assert _curated("staff.operator.address_saved", "en") not in " ".join(texts(operator)), (
        "the bot claimed a refused address had been saved"
    )
    assert _curated("staff.operator.select_address", "en") not in " ".join(texts(operator)), (
        "a refused address must not produce an address picker"
    )
    last_call = operator.backend.calls[-1]
    assert (last_call.method, last_call.endpoint) == ("POST", ADDRESSES_ENDPOINT), (
        "the refused write was followed by more work; a refusal must be the end of the flow, "
        f"not a step in it — trailing call was {last_call}"
    )
    assert operator.world.addresses == [], "nothing may have been persisted"
    assert operator.conversation_state("staff_add_address") is None
    assert operator.errors == []


# ---------------------------------------------------------------------------
# Role separation
# ---------------------------------------------------------------------------


async def test_a_delivery_driver_is_refused_at_every_operator_entry_point(driver):
    """A driver with an old message, a forwarded card, or a colleague's phone.

    The menu simply does not offer these flows to a driver, so the guards are a
    backstop — but backstops are what stale inline keyboards land on, and every
    one of these buttons stays tappable forever.

    Two properties matter equally. The driver must SEE a refusal (silence reads
    as a broken bot and they keep tapping), and the refused entry point must not
    park them in a conversation text state: a driver stuck in ``ENTER_PHONE``
    would have their next cash amount swallowed as a customer's phone number.
    """
    drv, labels = await sign_in(driver)

    for key in ("staff.menu.create_client", "staff.menu.search_client", "staff.menu.create_order"):
        value = _curated(key, "en")
        assert not [label for label in labels if label.strip().endswith(value)], (
            f"{value!r} must not appear on a delivery-driver menu"
        )

    stale_buttons = [
        "staff_create_client",
        "staff_search_client",
        "staff_create_order",
        f"staff_op_order_{CLIENT_ID}",
        f"staff_op_addresses_{CLIENT_ID}",
        f"staff_op_add_addr_{CLIENT_ID}",
    ]

    for data in stale_buttons:
        driver.telegram.reset()
        driver.backend.calls.clear()

        await driver.send(drv.tap(data))

        assert _curated("staff.unauthorized", "en") in alerts(driver), (
            f"tapping {data!r} as a driver produced no refusal at all: "
            f"alerts={alerts(driver)} shown={texts(driver)}"
        )
        assert driver.backend.calls == [], (
            f"{data!r} reached the backend as a driver: {driver.backend.calls}"
        )

    for name in ("staff_create_user", "staff_search_user", "staff_create_order", "staff_add_address"):
        assert driver.conversation_state(name) is None, (
            f"a refused driver was left parked in {name}; their next message will be eaten"
        )
    assert driver.errors == []


# ---------------------------------------------------------------------------
# Ratchets: current behaviour, pinned so it cannot get worse
# ---------------------------------------------------------------------------


async def test_the_first_address_added_for_a_client_lands_on_a_live_address_picker(operator):
    """A brand-new customer, given an order in one pass — the common case.

    ``start_order_for_client`` returned a STATE when the client had addresses
    but plain ``return`` (i.e. ``None``) when they had none. PTB's
    ``_update_state`` IGNORES ``None``, so the create-order conversation was
    never entered on that branch.

    ``confirm_address`` then noticed there was an order in progress and helpfully
    re-rendered the address picker — a keyboard whose ``staff_op_addr_<id>``
    pattern is registered ONLY inside ``ORDER_SELECT_ADDRESS``. Nothing claimed
    the tap. The operator, mid-call, had just typed four fields, been told the
    address was saved, and was looking at a button that did nothing at all; the
    only way forward was to start the whole order again.

    The no-addresses branch now returns ``SELECT_ADDRESS`` — the order genuinely
    IS at the address step, there just are not any addresses yet — so the picker
    it eventually renders is live and the tap carries the order on to products.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))

    assert operator.conversation_state("staff_create_order") == SELECT_ADDRESS, (
        "the no-addresses branch left the create-order conversation unentered, so "
        "every address button it draws is decorative"
    )

    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Ofis"))
    await operator.send(ops.text("Amir Temur ko'chasi 108"))
    await operator.send(ops.text("-"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_confirm_address"))

    picker = last_screen(operator)
    assert picker.text == _curated("staff.operator.select_address", "en")
    assert f"staff_op_addr_{NEW_ADDRESS_ID}" in picker.callback_data(), (
        "fixture: the freshly saved address should be offered"
    )

    live_tap = ops.tap(f"staff_op_addr_{NEW_ADDRESS_ID}")
    assert operator.handlers_matching(live_tap) != [], (
        "the address the operator just created reaches no handler — the picker is dead"
    )

    operator.telegram.reset()
    await operator.send(live_tap)

    assert backend_calls(operator, "GET", PRODUCTS_ENDPOINT) != [], (
        "tapping the new address did not advance the order to product selection"
    )
    assert operator.conversation_state("staff_create_order") == SELECT_PRODUCTS
    assert _curated("staff.operator.select_products", "en") in last_screen(operator).text, (
        f"the operator was not shown the product list; saw {texts(operator)}"
    )
    assert operator.errors == []


async def test_skipping_the_surname_shows_the_one_name_the_caller_gave(operator):
    """Regression: the success screen printed the literal word "None".

    Most callers give one name. ``receive_last_name`` stores ``None`` for "-",
    and ``confirm_create`` used to build the display name with
    ``f"{first_name} {last_name}".strip()``. ``.strip()`` removes whitespace,
    not the literal ``"None"`` that formatting a ``None`` produces — so the
    operator's success screen read "👤 Dilnoza None" and they read that line
    back to the caller.

    The screen ONE STEP EARLIER got it right (``if client_data.get('last_name')``),
    which was the tell: one rule about "does this customer have a surname"
    written twice, and only one copy handled the common case. Both screens now
    call ``build_client_display_name``, so this asserts BOTH of them — a third
    copy re-introducing the bug on either screen fails here.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await operator.send(ops.text("+998901112233"))
    await operator.send(ops.text("Dilnoza"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_lang_uz"))

    confirm = last_screen(operator)
    assert "Dilnoza" in confirm.text and "None" not in confirm.text, (
        "the confirmation screen must show the one name the caller gave"
    )

    await operator.send(ops.tap("staff_op_confirm_create_user"))

    done = last_screen(operator)
    assert "Dilnoza" in done.text, "the success screen must still name the customer"
    assert "None" not in done.text, (
        f"the success screen is reading a stray 'None' back to the caller: {done.text!r}"
    )
    # The write itself was never wrong; only what the operator read was.
    assert backend_calls(operator, "POST", CREATE_USER_ENDPOINT)[0].data["last_name"] is None


async def test_a_telegram_edit_failure_after_the_write_still_tells_the_operator_it_worked(
    operator,
):
    """Regression: a refused redraw was reported as a failed creation.

    ``confirm_create`` POSTs the customer and THEN edits the message to say so.
    Telegram refuses ``editMessageText`` routinely — "message is not modified",
    "message to edit not found" after 48 hours, a message the operator deleted
    — and that refusal used to land in the same ``except`` as a failed write.
    The operator was shown the generic "an error occurred" for a customer who
    WAS created, and the natural next move for a person mid-call is to create
    them again. They escaped a duplicate only incidentally, because the retry
    path is a phone lookup that now finds the row.

    The write and the redraw are different failures. Once the POST returns 2xx
    the only honest thing to say is that the customer exists — so the alert,
    which is the only surface left when the edit is refused, carries the same
    seeded ``staff.operator.user_created`` line the screen would have shown.
    """
    ops, labels = await sign_in(operator)
    operator.world.search_result = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_client")))
    await operator.send(ops.text("+998901112233"))
    await operator.send(ops.text("Dilnoza"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_lang_uz"))
    assert operator.conversation_state("staff_create_user") == CONFIRM_CREATE

    operator.telegram.reset()
    operator.telegram.fail("editMessageText", "Bad Request: message is not modified")

    await operator.send(ops.tap("staff_op_confirm_create_user"))

    assert len(backend_calls(operator, "POST", CREATE_USER_ENDPOINT)) == 1, (
        "the customer really was created"
    )
    # Telegram refused the success edit, so the ONLY thing that reached the
    # operator is this alert. It must not describe a write that succeeded as a
    # failure.
    assert alerts(operator) == [f"✅ {_curated('staff.operator.user_created', 'en')}"], (
        f"the operator was told something other than 'created'. alerts={alerts(operator)}"
    )
    assert _curated("staff.error_occurred", "en") not in alerts(operator), (
        "a refused redraw is being reported as a failed creation again"
    )
    assert operator.conversation_state("staff_create_user") is None
    assert "new_client" not in user_data(operator)
    assert not operator.errors, (
        f"the refused edit escaped as an unhandled error: {operator.errors}"
    )


async def test_an_operator_typed_address_outside_the_polygon_is_refused_before_it_is_saved(
    operator,
):
    """A caller reads out a Samarkand address and the operator types it in.

    ``shared.constants.TASHKENT_POLYGON`` is the delivery-zone SSOT and
    ``ensure_within_delivery_zone`` is the guard every coordinate-bearing write
    path funnels through. That guard is explicitly a NO-OP when either
    coordinate is missing — and this flow used to collect four lines of free
    text and no pin at all, so the polygon could not reject an operator-created
    address whatever it said. "Samarqand, Registon ko'chasi 5" became a
    deliverable address and the driver found out 280 km later.

    The typed line is now geocoded server-side through the same backend route
    the customer bot uses, so the SSOT gets a coordinate to speak about. This
    asserts the refusal lands on the operator, in their language, at the step
    they typed it — and that nothing at all was written.
    """
    assert not is_within_tashkent(*OUT_OF_ZONE_PIN), (
        "the polygon moved — pick a point the delivery-zone SSOT really rejects"
    )

    ops, labels = await sign_in(operator)
    operator.world.addresses = []
    operator.world.geocode = {
        "latitude": OUT_OF_ZONE_PIN[0],
        "longitude": OUT_OF_ZONE_PIN[1],
        "formatted_address": "Registon ko'chasi 5, Samarqand",
    }

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))
    operator.telegram.reset()

    await operator.send(ops.text("Samarqand, Registon ko'chasi 5"))

    geocoded = backend_calls(operator, "POST", GEOCODE_ENDPOINT)
    assert len(geocoded) == 1, (
        "the typed address was never geocoded, so the polygon had no coordinate to "
        f"judge; backend calls were {operator.backend.calls}"
    )
    assert geocoded[0].data == {"address": "Samarqand, Registon ko'chasi 5"}

    assert texts(operator)[-1] == _curated("staff.operator.outside_delivery_area", "en"), (
        f"the operator was not told the address is out of zone; saw {texts(operator)}"
    )
    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS, (
        "a refusal must land back on the address step so the operator can correct it "
        "while the caller is still on the line"
    )
    assert user_data(operator)["new_address"] == {"title": "Uy"}, (
        "an out-of-zone address must leave nothing behind to be confirmed later"
    )
    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT) == []
    assert operator.world.addresses == [], "nothing may have been persisted"
    assert operator.errors == []


async def test_an_in_zone_typed_address_is_saved_with_the_pin_the_zone_guard_ran_on(operator):
    """The same step, the happy path — the coordinates have to actually ship.

    Refusing Samarkand is only half the fix: if the pin the bot checked never
    reaches ``StaffService.add_client_address``, the row it writes still has
    NULL coordinates and ``ensure_within_delivery_zone`` is still a no-op for
    every later edit of that address. The zone check and the write have to be
    talking about the same point.

    The typed line itself stays exactly as dictated. A geocoder's
    ``formatted_address`` is coarser and would drop the floor and flat the
    caller just gave — which is what the driver reads at the door.
    """
    assert is_within_tashkent(*IN_ZONE_PIN), "control: the fixture pin must be inside"

    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Ofis"))
    await operator.send(ops.text("Amir Temur ko'chasi 108, 3-qavat"))
    await operator.send(ops.text("-"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_confirm_address"))

    writes = backend_calls(operator, "POST", ADDRESSES_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data["latitude"] == IN_ZONE_PIN[0]
    assert writes[0].data["longitude"] == IN_ZONE_PIN[1]
    assert writes[0].data["full_address"] == "Amir Temur ko'chasi 108, 3-qavat"
    assert operator.errors == []


async def test_an_operator_can_attach_a_pin_instead_of_typing_the_address(operator):
    """The other half of the choice the customer bot already offers.

    A pin is exact where a geocoder's reading of a sentence is a guess, so the
    zone guard runs on the real point. The bot draws the button that asks for
    it — ``request_location`` exists on ``KeyboardButton`` and nowhere else, so
    an inline keyboard can never ask — and the address step has to actually
    HANDLE the location that comes back. A shared pin reaching no handler would
    look identical to the bug this whole flow was fixed for: silence, and then
    an address with no coordinates.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))

    prompt = last_screen(operator)
    assert _curated("staff.operator.share_location", "en") in prompt.button_labels(), (
        f"no button asks for a pin, so the operator cannot attach one: {prompt.button_labels()}"
    )

    pin = ops.location(*IN_ZONE_PIN)
    assert operator.handlers_matching(pin) != [], (
        "a shared pin reaches no handler in the address step"
    )

    await operator.send(pin)
    assert operator.conversation_state("staff_add_address") == ENTER_DISTRICT

    reverse = backend_calls(operator, "POST", REVERSE_GEOCODE_ENDPOINT)
    assert len(reverse) == 1
    assert reverse[0].data == {"latitude": IN_ZONE_PIN[0], "longitude": IN_ZONE_PIN[1]}
    assert backend_calls(operator, "POST", GEOCODE_ENDPOINT) == [], (
        "a pin is already exact; geocoding it back into a guess is wasted work"
    )

    await operator.send(ops.text("-"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_confirm_address"))

    writes = backend_calls(operator, "POST", ADDRESSES_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data["latitude"] == IN_ZONE_PIN[0]
    assert writes[0].data["longitude"] == IN_ZONE_PIN[1]
    assert writes[0].data["full_address"] == operator.world.reverse_geocode["formatted_address"]
    assert operator.errors == []


async def test_a_pin_outside_the_polygon_is_refused_at_the_pin(operator):
    """An operator who taps "Send location" from home, or a mis-dropped pin.

    The refusal has to happen on the coordinates themselves. Reverse-geocoding
    an out-of-zone pin first and judging the resulting SENTENCE would put the
    delivery-zone decision back in the hands of a geocoder's formatting.
    """
    assert not is_within_tashkent(*OUT_OF_ZONE_PIN), (
        "the polygon moved — pick a point the delivery-zone SSOT really rejects"
    )

    ops, labels = await sign_in(operator)
    operator.world.addresses = []

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))
    operator.telegram.reset()

    await operator.send(ops.location(*OUT_OF_ZONE_PIN))

    assert texts(operator)[-1] == _curated("staff.operator.outside_delivery_area", "en")
    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS
    assert backend_calls(operator, "POST", REVERSE_GEOCODE_ENDPOINT) == [], (
        "an out-of-zone pin must be refused on its coordinates, not on its address text"
    )
    assert user_data(operator)["new_address"] == {"title": "Uy"}
    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT) == []
    assert operator.errors == []


async def test_an_address_the_geocoder_cannot_place_is_refused_rather_than_saved_blind(operator):
    """The geocoder shrugs. That is not permission to skip the zone check.

    An address saved with no coordinates is one the delivery-zone SSOT can
    never speak about again — not at write time, and not on any later edit of
    that row. So an unplaceable address is refused, and the operator is pointed
    at the pin button, which needs no geocoder at all.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []
    operator.world.geocode = staff_backend_failure("Address not found", status_code=404)

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))
    operator.telegram.reset()

    await operator.send(ops.text("Qayerdadir, kimningdir uyi"))

    assert texts(operator)[-1] == _curated("staff.operator.address_not_found", "en")
    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS
    assert _curated("staff.operator.share_location", "en") in last_screen(operator).button_labels()
    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT) == []
    assert operator.errors == []


async def test_a_pin_whose_address_cannot_be_read_back_is_completed_by_typing_it(operator):
    """Geocoder down, caller waiting. The pin is still exact.

    This is the one path that must not become a dead end: refusing the typed
    address AND refusing the pin would leave an operator on a live call unable
    to add any address at all while the geocoder is unavailable. The pin has
    already been zone-checked, so the typed line that follows is stored as-is —
    and, critically, is NOT re-geocoded, because a second failure there would
    close the only door left open.
    """
    ops, labels = await sign_in(operator)
    operator.world.addresses = []
    operator.world.reverse_geocode = staff_backend_failure(
        "Geocoding service unavailable", status_code=503
    )
    operator.world.geocode = staff_backend_failure(
        "Geocoding service unavailable", status_code=503
    )

    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    await operator.send(ops.text("Uy"))

    await operator.send(ops.location(*IN_ZONE_PIN))
    assert texts(operator)[-1] == _curated("staff.operator.location_needs_address", "en")
    assert operator.conversation_state("staff_add_address") == ENTER_ADDRESS

    await operator.send(ops.text("Amir Temur ko'chasi 108, 3-qavat"))
    assert operator.conversation_state("staff_add_address") == ENTER_DISTRICT

    await operator.send(ops.text("-"))
    await operator.send(ops.text("-"))
    await operator.send(ops.tap("staff_op_confirm_address"))

    writes = backend_calls(operator, "POST", ADDRESSES_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data["latitude"] == IN_ZONE_PIN[0]
    assert writes[0].data["longitude"] == IN_ZONE_PIN[1]
    assert writes[0].data["full_address"] == "Amir Temur ko'chasi 108, 3-qavat"
    assert operator.errors == []
