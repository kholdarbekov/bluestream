"""Every bottle a driver moves is a number they typed on a phone in a van.

There is no barcode scanner in this business. The 18.9-litre bottles that
leave the warehouse, come back from a customer's door, or move from one truck
to another are counted by a human and typed into Telegram. Whatever reaches
``/api/v1/staff/bottles/*`` becomes the inventory ledger, and the ledger is
what the driver is later held financially accountable for.

So the interesting failures are never "the handler function returned the wrong
dict". They are:

* a count that was typed but never posted, or posted twice;
* a prompt that stayed armed after the driver walked away, so the next
  unrelated thing they typed became a quantity (the state-leak class this
  project has already shipped twice — see
  ``project_staff_bot_text_router_state_leak`` and
  ``project_bottle_session_late_bind``);
* a button on a message from ten minutes ago that still writes.

Every test in this file therefore drives the REAL dispatcher: ``/start``,
a reply-keyboard tap read off the keyboard the bot itself drew, then inline
taps read off ``callback_data`` the bot itself rendered. Assertions are on the
exact bytes that reached the backend and the exact copy the driver saw, plus
``harness.conversation_state(...)`` — because "which prompt am I still parked
in?" is the thing that silently rots.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.delivery.bottle_collection import (
    BOTTLE_COLLECTION_SEARCH_INPUT,
    BOTTLE_SESSION_LOADED_QTY_INPUT,
    BOTTLE_SESSION_RETURNED_QTY_INPUT,
    BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT,
    BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT,
    BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT,
    BOTTLE_TRANSFER_DRIVER_SELECT,
    BOTTLE_TRANSFER_EMPTY_QTY_INPUT,
    BOTTLE_TRANSFER_FULL_QTY_INPUT,
    BOTTLE_TRANSFER_SEND_CHOICE,
    BOTTLE_TRANSFER_UNIT_QTY_INPUT,
)
from staff_bot.utils.formatters import format_quantity
from staff_bot.utils.search import detect_search_type

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Real copy, resolved live from the seed script
# ---------------------------------------------------------------------------
# Same technique as tests/staff_bot/test_staff_menu_routing_journey.py: the
# strings are pulled through `_curated_value`, the SAME function
# `seed_translations()` calls, instead of being pasted here. Pasting would let
# a future edit to the seed leave this file asserting copy production no
# longer ships — i.e. testing the test.

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

# The main-menu reply keyboard. Seeded in every language on purpose: the
# conversation menu-escape (`MenuTapFilter`, via `_resolve_tapped_label`) sweeps
# ALL of them on every tap, so a table that only had English would be testing a
# different router than production runs.
MENU_KEYS = (
    "staff.menu.title",
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
)

# Everything the bottle screens render.
BOTTLE_KEYS = (
    "staff.bottles.join_session",
    "staff.bottles.choose_session_to_join",
    "staff.bottles.join_session_confirm_title",
    "staff.bottles.join_session_confirm_note",
    "staff.bottles.confirm_join",
    "staff.bottles.session_owner",
    "staff.bottles.full_on_truck",
    "staff.bottles.empties_on_truck",
    "staff.bottles.free_full_bottles",
    "staff.bottles.no_open_sessions",
    "staff.bottles.join_request_sent",
    "staff.bottles.join_request_approved_owner",
    "staff.bottles.join_request_declined_owner",
    "staff.error.api.bottle_session_closed",
    "staff.error.api.join_request_stale",
    "staff.error.api.transfer_receiver_invalid",
    "staff.back",
    "staff.cancel",
    "staff.cash.hub_title",
    "staff.menu.cash_reconciliation",
    "staff.menu.collect_cod_debt",
    "staff.menu.bottle_collection",
    "staff.menu.log_bottles_loaded",
    "staff.menu.return_to_warehouse",
    "staff.menu.my_bottle_accountability",
    "staff.menu.transfer_bottles_to_driver",
    "staff.menu.incoming_transfers",
    "staff.delivery.enter_bottles_loaded_qty",
    "staff.delivery.enter_bottles_returned_qty",
    "staff.delivery.invalid_bottle_count",
    "staff.delivery.invalid_count_zero_ok",
    "staff.delivery.enter_fine_bottle_qty",
    "staff.delivery.enter_fine_amount",
    "staff.delivery.bottle_session_opened_header",
    "staff.delivery.session_bottles_line",
    "staff.delivery.session_unit_line",
    "staff.delivery.enter_unit_loaded_qty",
    "staff.delivery.enter_unit_returned_qty",
    "staff.delivery.discrepancy_unit_zero",
    "staff.delivery.discrepancy_unit_line",
    "staff.bottles.unit_on_truck",
    "staff.error.api.bottle_session_return_incomplete",
    "staff.error.api.membership_already_active_owner_detail",
    "staff.error.api.bottle_session_empty_load",
    "staff.delivery.bottle_session_closed",
    "staff.delivery.bottle_session_already_open",
    "staff.delivery.bottle_session_already_open_close_first",
    "staff.delivery.no_active_bottle_session",
    "staff.delivery.discrepancy_zero",
    "staff.delivery.discrepancy_nonzero",
    # The session summary block rendered above the return prompt.
    "staff.delivery.session_ref_label",
    "staff.delivery.session_started_label",
    "staff.delivery.bottles_loaded_label",
    "staff.delivery.bottles_delivered_label",
    "staff.delivery.bottles_collected_label",
    "staff.delivery.bottles_transferred_out_split",
    "staff.delivery.bottles_transferred_in_split",
    "staff.delivery.bottles_on_truck_label",
    "staff.delivery.bottle_session_status.open",
    "staff.delivery.bottle_collection_search_prompt",
    "staff.delivery.bottle_search_results_title",
    "staff.delivery.no_customer_bottle_results",
    "staff.delivery.bottle_statement_title",
    "staff.delivery.total_bottles",
    "staff.delivery.no_bottle_balance",
    "staff.delivery.collect_bottles",
    "staff.delivery.collect_all",
    "staff.delivery.issue_bottle_fine",
    "staff.delivery.enter_bottle_collection_qty",
    "staff.delivery.enter_bottle_collection_note",
    "staff.delivery.bottle_collection_recorded",
    "staff.delivery.save_without_note",
    "staff.delivery.select_transfer_receiver",
    "staff.delivery.transfer_send_prompt",
    "staff.delivery.transfer_send_all_button",
    "staff.delivery.transfer_choose_amounts_button",
    "staff.delivery.enter_transfer_full_qty",
    "staff.delivery.enter_transfer_empty_qty",
    "staff.delivery.bottle_transfer_sent_lines",
    "staff.delivery.enter_transfer_unit_qty",
    "staff.delivery.transfer_line_full",
    "staff.delivery.transfer_line_empty",
    "staff.delivery.transfer_line_product",
    "staff.delivery.session_unit_line",
    "staff.error.api.transfer_exceeds_unit_detail",
    "staff.delivery.no_bottles_to_transfer",
    "staff.delivery.no_active_drivers",
    "staff.delivery.pending_transfers_title",
    "staff.delivery.pending_transfer_lines_line",
    "staff.delivery.transfer_confirm_lines_button",
    "staff.delivery.transfer_custom_count_button",
    "staff.delivery.transfer_reject_button",
    "staff.delivery.transfer_reject_confirm",
    "staff.delivery.transfer_reject_yes",
    "staff.delivery.transfer_reject_back",
    "staff.delivery.transfer_rejected_reply",
    "staff.delivery.enter_received_full_qty",
    "staff.delivery.enter_received_empty_qty",
    "staff.delivery.enter_received_unit_qty",
    "staff.delivery.transfer_confirmed_lines",
    "staff.delivery.transfer_disputed_lines",
    "staff.delivery.transfer_disputed_lines_no_admin",
    "staff.bottles.notify.transfer_received_lines",
    "staff.operator.search_too_short",
    "staff.error.api.service_unavailable",
    "staff.error.api.validation",
    "staff.error.api.transfer_exceeds_inventory_detail",
    "staff.error.api.transfer_exceeds_full_detail",
    "staff.error.api.transfer_exceeds_empty_detail",
    "staff.error.api.transfer_nothing_to_send",
    "staff.error.api.transfer_already_handled",
    "staff.error.api.transfer_not_yours",
    "staff.error.api.joined_session_closed",
    "staff.error.api.open_session_to_invite",
    "staff.error_occurred",
    "staff.session_expired",
    "staff.cancelled",
    "staff.flow_timed_out",
    # Co-driver session membership: two trucks, one inventory.
    "staff.bottles.current_membership_title",
    "staff.bottles.leave_session",
    "staff.bottles.left_session",
    "staff.error.api.leave_has_promised_stops",
    "staff.error.api.leave_has_promised_stops_detail",
    "staff.bottles.no_active_membership",
    "staff.bottles.invite_codriver",
    "staff.bottles.choose_driver_to_invite",
    "staff.bottles.no_drivers_to_invite",
    "staff.common.unknown_driver",
    "staff.common.driver_number",
)


# A slip at a count step where 0 is a real answer (Load, Return, Send, the receiver's count, the empties at the
# door). The fine quantity, which must be at least 1, keeps ``staff.delivery.invalid_bottle_count``.
ZERO_OK_SLIP = "staff.delivery.invalid_count_zero_ok"


def _curated(key: str, language: str = "en") -> str:
    value = _SEED._curated_value(key, language)
    assert value, (
        f"{key} has no curated {language} value in scripts/seed_staff_translations.py — "
        "production would render a humanised placeholder for it, and this test would "
        "be asserting against that placeholder rather than the copy a driver sees"
    )
    return value


def _translation_table(overrides: dict = None) -> dict:
    table = {}
    for key in MENU_KEYS + BOTTLE_KEYS:
        for language in LANGUAGES:
            table[(language, key)] = _curated(key, language)
    table.update(overrides or {})
    return table


# ---------------------------------------------------------------------------
# Backend surface
# ---------------------------------------------------------------------------

LOGIN = "/api/v1/staff/auth/login"
SESSION_CURRENT = "/api/v1/staff/bottles/session/current"
SESSION_OPEN = "/api/v1/staff/bottles/session/open"
LOAD_SCREEN = "/api/v1/staff/bottles/session/load-screen"
SESSION_CLOSE = "/api/v1/staff/bottles/session/close"
AVAILABLE_DRIVERS = "/api/v1/staff/bottles/sessions/available-drivers"
TRANSFER_RECIPIENTS = "/api/v1/staff/bottles/transfers/recipients"
JOINABLE = "/api/v1/staff/bottles/sessions/joinable"
JOIN_REQUEST = "/api/v1/staff/bottles/session/join-request"
JOIN_APPROVE = f"{JOIN_REQUEST}/approve"
JOIN_DECLINE = f"{JOIN_REQUEST}/decline"
# `serialize_joinable_session`: 7 full and 2 empties on the owner's truck, 3 of the full free.
JOINABLE_ROW = {
    "session_id": 12, "owner_name": "Ali <&> Co", "current_inventory": 9, "bottles_loaded": 40,
    "full_bottles_on_board": 7, "empties_on_board": 2, "free_full_bottles": 3,
    "transferable_full": 7, "transferable_empties": 2,
}
# A second truck, every number distinct from the first: 11 full and 30 empties (41 on board, 41 loaded),
# 5 of the full free. Before the split its list button read "41/41", a full load.
JOINABLE_ROW_2 = {
    "session_id": 14, "owner_name": "Bek Toshev", "current_inventory": 41, "bottles_loaded": 41,
    "full_bottles_on_board": 11, "empties_on_board": 30, "free_full_bottles": 5,
    "transferable_full": 11, "transferable_empties": 30,
}
# `serialize_membership_session_info` for the same truck: the colleague's session the driver works under.
MEMBERSHIP_ROW = {
    "membership_id": 3, "session_id": 14, "owner_name": "Bek Toshev", "current_inventory": 41,
    "bottles_loaded": 41, "full_bottles_on_board": 11, "empties_on_board": 30, "free_full_bottles": 5,
    "transferable_full": 11, "transferable_empties": 30, "status": "active",
}
TRANSFERS = "/api/v1/staff/bottles/transfers"
TRANSFERS_PENDING = "/api/v1/staff/bottles/transfers/pending"
SESSION_MEMBERSHIP = "/api/v1/staff/bottles/session/membership"
SESSION_LEAVE = "/api/v1/staff/bottles/session/leave"
CUSTOMER_SEARCH = "/api/v1/staff/customers/search"
COLLECTION = "/api/v1/staff/bottles/collection"

CUSTOMER_ID = 501
ADDRESS_ID = 9001
RECEIVER_DRIVER_ID = 77
TRANSFER_ID = 31

CUSTOMER_SUMMARY = f"/api/v1/staff/bottles/customer/{CUSTOMER_ID}/summary"
CUSTOMER_ADDRESSES = f"/api/v1/staff/bottles/customer/{CUSTOMER_ID}/addresses"
TRANSFER_CONFIRM = f"/api/v1/staff/bottles/transfers/{TRANSFER_ID}/confirm"
TRANSFER_REJECT = f"/api/v1/staff/bottles/transfers/{TRANSFER_ID}/reject"
# A second push in the same chat: another colleague's handover, waiting on its own.
OTHER_TRANSFER_ID = 32
OTHER_TRANSFER_CONFIRM = f"/api/v1/staff/bottles/transfers/{OTHER_TRANSFER_ID}/confirm"


def open_session(**overrides) -> dict:
    """A driver mid-shift, as `serialize_bottle_session` publishes it: 40 loaded,
    26 delivered, 5 empties collected, so 14 full and 5 empties on the truck.
    Eight of the 14 full are promised to his On-the-way stops, leaving 6 free."""
    session = {
        "id": 12,
        "session_ref": "SES-2026-08-21-AZIZ",
        "status": "open",
        "started_at": "2026-08-21T06:30:00+00:00",
        "bottles_loaded": 40,
        "bottles_delivered": 26,
        "bottles_collected_from_customers": 5,
        "bottles_transferred_out": 0,
        "bottles_transferred_in": 0,
        "empties_transferred_out": 0,
        "empties_transferred_in": 0,
        "full_bottles_on_board": 14,
        "empties_on_board": 5,
        "free_full_bottles": 6,
        "transferable_full": 14,
        "transferable_empties": 5,
        "current_inventory": 19,
        "transferable_any": True,
        "units": [],
    }
    session.update(overrides)
    return session


def load_screen(*, products=(), refusal=None, open_session=None, **extra) -> dict:
    """`serialize_load_screen`: the products a load number is asked for, and the refusal when no trip may open."""
    return {"products": list(products), "refusal": refusal, "open_session": open_session, **extra}


TEN_LITRE = {"product_id": 3, "product_name": "10 Litr suv"}


def unit_line(**overrides) -> dict:
    """One `units` entry exactly as `_unit_lines` publishes it, every key present."""
    line = {
        "product_id": 3, "product_name": "10 Litr suv", "loaded": 12, "delivered": 0, "transferred_in": 0,
        "transferred_out": 0, "on_board": 12, "transferable": 12, "free": 12, "returned": None, "discrepancy": None,
    }
    line.update(overrides)
    return line


PLACE_ROW = {
    "address_id": ADDRESS_ID,
    "address_title": "Chilonzor 12",
    "is_grouped": False,
    "place_balance": 4,
}

BOTTLE_SUMMARY = {
    "customer_id": CUSTOMER_ID,
    "cluster_scopes": [{"scope_key": f"a:{ADDRESS_ID}", "balance": 4}],
    "active_fines_count": 0,
    "total_fine_amount": 0,
    "addresses": [PLACE_ROW],
}


def _staff_row(language="en"):
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


def _login_payload(language="en"):
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


async def build_driver(monkeypatch, *, language="en", translations=None):
    """A logged-in delivery driver whose bottle endpoints all answer sanely.

    ``session/current`` is routed EXPLICITLY even where a test does not care:
    the harness' unrouted default answers ``{"data": {}}``, which every session
    handler reads as "there is already an open session". An unrouted endpoint
    would therefore silently flip half the flows into their blocked arm.
    """
    harness = await build_staff_harness(
        monkeypatch,
        translations=translations if translations is not None else _translation_table(),
        database=FakeStaffDatabase(staff_user=_staff_row(language)),
    )
    harness.backend.route("POST", LOGIN, lambda _call: _login_payload(language))
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: None)
    harness.backend.route("GET", LOAD_SCREEN, lambda _call: load_screen())
    return harness


@pytest.fixture
async def driver(monkeypatch):
    return await build_driver(monkeypatch)


# ---------------------------------------------------------------------------
# Driving helpers
# ---------------------------------------------------------------------------


async def sign_in(harness):
    """Run the real ``/start`` login; return the update factory and the labels
    on the reply keyboard the bot actually drew."""
    driver_updates = harness.updates()
    await harness.send(driver_updates.command("start"))
    shown = harness.telegram.shown
    assert shown, "/start produced nothing — the driver sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    harness.telegram.reset()
    return driver_updates, labels


def menu_label(labels, key, language="en"):
    """The ONE rendered reply-keyboard label carrying this translation.

    Matched on the translated value rather than rebuilt as f"{emoji} {value}",
    so the emoji stays an implementation detail of the keyboard.
    """
    value = _curated(key, language)
    hits = [label for label in labels if label.strip().endswith(value)]
    assert len(hits) == 1, f"expected exactly one menu button carrying {value!r}, got {hits}"
    return hits[0]


async def open_cash_hub(harness, driver_updates, labels, language="en"):
    """Tap 💰 Cash on the reply keyboard and return the hub's callback data."""
    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash", language)))
    hub = harness.telegram.last_shown()
    assert _curated("staff.cash.hub_title", language) in hub.text
    harness.telegram.reset()
    return hub.callback_data()


def backend_calls(harness, method, endpoint):
    return [
        call
        for call in harness.backend.calls
        if call.method == method.upper() and call.endpoint == endpoint
    ]


def capture_errors(harness) -> list:
    """Collect every exception PTB would otherwise have swallowed into a log.

    Without a registered error handler a raising handler is indistinguishable
    from one that quietly did nothing — and "quietly did nothing" is precisely
    the failure mode this file is about.
    """
    errors = []

    async def _record(_update, context):
        errors.append(context.error)

    harness.application.add_error_handler(_record)
    return errors


def user_data(harness):
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


# ===========================================================================
# Loading bottles at the warehouse
# ===========================================================================


async def test_a_driver_loads_forty_bottles_at_the_warehouse_and_that_exact_count_reaches_the_backend(driver):
    """The whole load journey, from the reply keyboard to the POST body.

    This is the write that starts a driver's financial accountability for the
    day: everything they later deliver, collect or return is reconciled against
    it. If the count the driver typed is not the count in the POST — or the
    prompt never appears because the button is wired to nothing — the driver
    leaves the depot with an inventory the system does not know about, and the
    end-of-day discrepancy is charged to them.
    """
    driver_updates, labels = await sign_in(driver)
    hub_buttons = await open_cash_hub(driver, driver_updates, labels)
    assert "staff_bottle_log_loaded" in hub_buttons, (
        f"the cash hub no longer offers the load-bottles button: {hub_buttons}"
    )

    driver.backend.route("POST", SESSION_OPEN, lambda _call: open_session(bottles_loaded=40))

    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))
    assert driver.telegram.last_shown().text == _curated("staff.delivery.enter_bottles_loaded_qty")
    assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT

    driver.telegram.reset()
    await driver.send(driver_updates.text("40"))

    posts = backend_calls(driver, "POST", SESSION_OPEN)
    assert len(posts) == 1, f"expected exactly one session-open write, got {posts}"
    assert posts[0].data == {"bottles_loaded": 40}, (
        "the count that reached the backend is not the count the driver typed"
    )

    assert driver.telegram.last_shown().text == (
        f"{_curated('staff.delivery.bottle_session_opened_header').format(ref='SES-2026')}\n"
        f"{_curated('staff.delivery.session_bottles_line').format(n=40)}"
    )
    assert driver.conversation_state("staff_bottle_loaded") is None, (
        "the quantity prompt is still armed after a successful load; the driver's "
        "next message would be parsed as a second bottle count"
    )


async def test_a_bottle_count_that_is_not_a_number_is_refused_and_the_driver_can_simply_retype_it(driver):
    """Fat fingers in a cold warehouse: "fourty", "4o", "40 bottles".

    Two things must both hold, and they pull in opposite directions. Nothing
    may be posted — a garbled count silently rounded to something is an
    inventory lie. And the conversation must STAY on the prompt, because a
    flow that ends on a typo forces the driver back through the whole menu and
    is the reason drivers stop using the bot and phone the operator instead.
    """
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    driver.backend.route("POST", SESSION_OPEN, lambda _call: open_session(bottles_loaded=40))
    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))

    for typo in ("fourty", "4o", "40 bottles", "40.5", "  "):
        driver.telegram.reset()
        await driver.send(driver_updates.text(typo))
        assert driver.telegram.last_shown().text == _curated(ZERO_OK_SLIP), (
            f"{typo!r} was not refused with the invalid-count copy"
        )
        assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT, (
            f"{typo!r} knocked the driver out of the quantity prompt"
        )
        assert not backend_calls(driver, "POST", SESSION_OPEN), (
            f"{typo!r} reached the backend as a bottle count"
        )

    driver.telegram.reset()
    await driver.send(driver_updates.text("40"))
    assert [call.data for call in backend_calls(driver, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": 40}
    ]


async def test_a_negative_load_never_leaves_the_bot(driver):
    """"-5" and "-1" are typos or a driver testing the bot: a negative load would make the ledger read as if the
    warehouse owed the driver bottles. Refused at the keypad, in the driver's words, with the prompt still armed.
    (Zero is an answer now, see `test_zero_and_zero_reaches_the_backend_and_its_refusal_asks_again`.)"""
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))

    for refused in ("-5", "-1"):
        driver.telegram.reset()
        await driver.send(driver_updates.text(refused))
        assert driver.telegram.last_shown().text == _curated(ZERO_OK_SLIP)
        assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT

    assert not backend_calls(driver, "POST", SESSION_OPEN), "a negative bottle count was posted to the ledger"


async def test_an_absurd_bottle_count_is_refused_at_the_keypad_not_at_the_column(driver):
    """A slipped finger on the keypad must be answered, not turned into a 500.

    ``receive_bottles_loaded`` guarded the LOWER bound only (``count <= 0``).
    Python's ``int()`` has no upper bound, and neither does
    ``DriverBottleSessionOpenRequest`` (``Field(..., gt=0)``), so a typed phone
    number — "40000000000" — sailed through the bot and pydantic and landed on
    ``DriverBottleSession.bottles_loaded``, a 4-byte PostgreSQL ``Integer``
    whose ceiling is 2147483647. The insert raised a DataError, which reached
    the driver standing at the depot as a generic 500 with no hint about what
    was wrong with what they typed.

    The count is now bounded in the bot by ``MAX_BOTTLES_PER_SESSION`` and
    answered with ``staff.delivery.invalid_count_zero_ok``, exactly as the lower
    bound already is — and the prompt stays armed so the driver can simply
    retype it.
    """
    from business_app.models.bottle import DriverBottleSession
    from staff_bot.handlers.delivery.bottle_collection import MAX_BOTTLES_PER_SESSION

    column_ceiling = 2**31 - 1
    assert DriverBottleSession.__table__.c.bottles_loaded.type.python_type is int, (
        "bottles_loaded is no longer an integer column — re-derive this bound"
    )
    assert 0 < MAX_BOTTLES_PER_SESSION < column_ceiling, (
        "the bot's ceiling must sit below what the storage column can hold"
    )

    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    driver.backend.route("POST", SESSION_OPEN, lambda _call: open_session())
    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))

    driver.telegram.reset()
    await driver.send(driver_updates.text("40000000000"))

    assert not backend_calls(driver, "POST", SESSION_OPEN), (
        "a count the storage column cannot hold was posted anyway"
    )
    assert driver.telegram.last_shown().text == _curated(ZERO_OK_SLIP), (
        "the driver was not told what was wrong with what they typed"
    )
    assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT, (
        "the refusal dropped the driver out of the prompt they still have to answer"
    )

    # And the ceiling itself is a legal load-out: this bound exists to catch
    # typos, not to turn away a truck that really is full.
    await driver.send(driver_updates.text(str(MAX_BOTTLES_PER_SESSION)))
    assert [call.data for call in backend_calls(driver, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": MAX_BOTTLES_PER_SESSION}
    ], "a load-out at the ceiling was refused, or posted as something else"


async def test_tapping_the_cash_menu_button_while_the_bot_waits_for_a_bottle_count_navigates_away_and_disarms_the_prompt(driver):
    """The driver changes their mind halfway and taps a main-menu button.

    The reply keyboard is permanently on screen and sends plain TEXT, so
    without the menu-escape handler prepended to this state the tap is eaten by
    the state's own quantity parser. That is the documented state-leak class
    (``project_staff_bot_text_router_state_leak``): the conversation stays
    armed, and the driver's NEXT message — a COD amount, a note, anything —
    silently becomes a bottle count and opens a session nobody asked for.

    So the tap must (a) navigate, (b) END the conversation, and (c) leave
    nothing behind that would parse the next message.
    """
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    driver.backend.route("POST", SESSION_OPEN, lambda _call: open_session())
    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))
    assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT

    driver.telegram.reset()
    await driver.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

    assert driver.conversation_state("staff_bottle_loaded") is None, (
        "the abandoned quantity prompt is still armed and will swallow the driver's "
        "next unrelated message as a bottle count"
    )
    assert _curated("staff.cash.hub_title") in driver.telegram.last_shown().text, (
        "the menu tap did not navigate anywhere"
    )

    # The proof that it is really disarmed: a bare number afterwards.
    driver.telegram.reset()
    await driver.send(driver_updates.text("40"))
    assert not backend_calls(driver, "POST", SESSION_OPEN), (
        "a number typed AFTER the driver left the flow still opened a bottle session"
    )


async def test_a_driver_who_already_has_an_open_session_is_told_so_and_is_never_asked_for_a_count(monkeypatch):
    """One truck, one open session. Tapping Load twice must not start a second.

    This is also the trap the bot shipped once already: the blocked arm has to
    END the conversation, not merely render a message. When it did not, the
    driver was told "you already have a session" and then had every subsequent
    message parsed as a bottle count — the bot appeared to keep asking a
    question it had just refused to ask.
    """
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", LOAD_SCREEN, lambda _call: load_screen(
        refusal={
            "error_code": "BOTTLE_SESSION_ALREADY_OPEN", "owner_name": "Aziz", "session_id": 12,
            "bottles_on_board": 14, "units": [],
        },
        open_session=open_session(),
    ))
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session())

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    # A trip with no unit line reads exactly as it did before 10 L was counted on the trip.
    assert harness.telegram.last_shown().text == (
        "🚫 <b>Cannot start a new load.</b>\n\n"
        "You already have an active session started at <b>2026-08-21 06:30</b> with <b>40</b> (18.9 L) bottles "
        "loaded.\n\n"
        "Return to the warehouse and close your current session first."
    )
    assert harness.conversation_state("staff_bottle_loaded") is None, (
        "the refusal left the quantity prompt armed"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.text("40"))
    assert not backend_calls(harness, "POST", SESSION_OPEN), (
        "a number typed after the refusal opened a second session for the same truck"
    )


async def uz_driver_with_the_ten_litre_load(monkeypatch, **screen):
    harness = await build_driver(monkeypatch, language="uz")
    harness.backend.route("GET", LOAD_SCREEN, lambda _call: load_screen(**{"products": [TEN_LITRE], **screen}))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels, "uz")
    return harness, driver_updates, labels


async def test_the_load_asks_both_numbers_and_opens_once_with_both(monkeypatch):
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(monkeypatch)
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session(
        session_ref="a1b2c3d4-0000", bottles_loaded=40, units=[unit_line(loaded=12)],
    ))

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_bottles_loaded_qty", "uz")
    await harness.send(driver_updates.text("40"))
    assert harness.telegram.last_shown().text == "Ombordan nechta 10 Litr suv yuklab olganingizni kiriting:"
    assert not backend_calls(harness, "POST", SESSION_OPEN), "the load was posted before the second number"
    assert harness.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT
    await harness.send(driver_updates.text("12"))

    assert [call.data for call in backend_calls(harness, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": 40, "units": [{"product_id": 3, "quantity": 12}]}
    ]
    assert harness.telegram.last_shown().text == (
        "✅ Reys ochildi (#a1b2c3d4)\n💧 18,9 L to'la idish: 40\n🧴 10 Litr suv: 12"
    )
    assert harness.conversation_state("staff_bottle_loaded") is None
    assert "pending_load_draft" not in user_data(harness)


async def test_zero_and_zero_reaches_the_backend_and_its_refusal_asks_again(monkeypatch):
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(monkeypatch)
    replies = iter([
        staff_backend_failure("empty load", 400, error_code="BOTTLE_SESSION_EMPTY_LOAD", error_type="VALIDATION_ERROR"),
        open_session(session_ref="a1b2c3d4-0000", bottles_loaded=0, units=[unit_line(loaded=5)]),
    ])
    harness.backend.route("POST", SESSION_OPEN, lambda _call: next(replies))

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    harness.telegram.reset()
    await harness.send(driver_updates.text("0"))
    await harness.send(driver_updates.text("0"))

    assert [call.data for call in backend_calls(harness, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": 0, "units": [{"product_id": 3, "quantity": 0}]}
    ]
    texts = [shown.text for shown in harness.telegram.shown]
    assert texts[-2:] == [
        "❌ " + _curated("staff.error.api.bottle_session_empty_load", "uz"),
        _curated("staff.delivery.enter_bottles_loaded_qty", "uz"),
    ]
    assert harness.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT

    await harness.send(driver_updates.text("0"))
    await harness.send(driver_updates.text("5"))
    assert [call.data for call in backend_calls(harness, "POST", SESSION_OPEN)][1:] == [
        {"bottles_loaded": 0, "units": [{"product_id": 3, "quantity": 5}]}
    ]
    assert harness.telegram.last_shown().text == (
        "✅ Reys ochildi (#a1b2c3d4)\n💧 18,9 L to'la idish: 0\n🧴 10 Litr suv: 5"
    )


async def test_a_co_driver_at_load_is_told_whose_trip_and_its_lines_and_asked_nothing(monkeypatch):
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(
        monkeypatch,
        refusal={
            "error_code": "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE", "owner_name": "Aziz", "session_id": 9,
            "bottles_on_board": 40, "units": [unit_line(on_board=8, loaded=12)],
        },
    )
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session())

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    assert harness.telegram.last_shown().text == (
        "Siz Azizning reysidasiz. O'z reysingizni ochish uchun avval undan chiqing.\n"
        "💧 18,9 L to'la idish: 40\n🧴 10 Litr suv: 8"
    )
    assert harness.conversation_state("staff_bottle_loaded") is None
    await harness.send(driver_updates.text("40"))
    assert not backend_calls(harness, "POST", SESSION_OPEN)


async def test_a_co_driver_refused_at_load_can_go_from_the_refusal_to_leaving(monkeypatch):
    """The refusal tells a co-driver to leave the trip first, so it carries the way there: the membership screen,
    whose Leave posts the leave. The plain session menu under it had none. Join is not offered: he is on a trip."""
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(
        monkeypatch,
        refusal={
            "error_code": "BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE", "owner_name": "Bek Toshev", "session_id": 14,
            "bottles_on_board": 11, "units": [],
        },
    )
    harness.backend.route("GET", SESSION_MEMBERSHIP, lambda _call: dict(MEMBERSHIP_ROW))
    harness.backend.route("POST", SESSION_LEAVE, lambda _call: {"status": "left"})

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    refusal = harness.telegram.last_shown()
    assert refusal.text.startswith("Siz Bek Toshevning reysidasiz. O'z reysingizni ochish uchun avval undan chiqing.")
    assert refusal.callback_data() == [
        "staff_bottle_session_load", "staff_bottle_session_return", "staff_bottle_transfer_start",
        "staff_bottle_transfers_pending", "bottles_membership_status", "staff_cash_hub",
    ]
    assert f"🤝 {_curated('staff.bottles.current_membership_title', 'uz')}" in refusal.button_labels()

    await harness.send(driver_updates.tap("bottles_membership_status"))
    assert "bottles_leave_session" in harness.telegram.last_shown().callback_data()
    await harness.send(driver_updates.tap("bottles_leave_session"))

    assert len(backend_calls(harness, "POST", SESSION_LEAVE)) == 1
    assert harness.telegram.last_shown().text == f"✅ {_curated('staff.bottles.left_session', 'uz')}"


async def test_already_open_lists_every_line(monkeypatch):
    """Every line sits under the 18.9 L loaded figure, and the instruction to close the trip stays the last
    paragraph: a 10 L line printed after it read as if it belonged to the instruction."""
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(
        monkeypatch,
        refusal={
            "error_code": "BOTTLE_SESSION_ALREADY_OPEN", "owner_name": "Aziz", "session_id": 12,
            "bottles_on_board": 14,
            "units": [unit_line(loaded=12), unit_line(product_id=4, product_name="5 Litr <suv>", loaded=7)],
        },
        open_session=open_session(),
    )

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    assert harness.telegram.last_shown().text == (
        "🚫 <b>Yangi yuklash boshlash mumkin emas.</b>\n\n"
        "Sizda allaqachon <b>2026-08-21 06:30</b> da boshlangan va <b>40</b> ta (18,9 l) idish yuklangan "
        "faol sessiya mavjud.\n"
        "🧴 10 Litr suv: 12\n"
        "🧴 5 Litr &lt;suv&gt;: 7\n\n"
        "Avval omborga qayting va joriy sessiyangizni yoping."
    )
    assert harness.conversation_state("staff_bottle_loaded") is None


async def test_load_reads_the_load_screen_once_and_ignores_keys_it_does_not_know(monkeypatch):
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(monkeypatch, vehicles=[{"id": 1}])

    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_bottles_loaded_qty", "uz")
    assert len(backend_calls(harness, "GET", LOAD_SCREEN)) == 1
    assert not backend_calls(harness, "GET", SESSION_CURRENT)


async def test_a_menu_tap_during_the_ten_litre_prompt_drops_the_draft(monkeypatch):
    harness, driver_updates, labels = await uz_driver_with_the_ten_litre_load(monkeypatch)
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session())
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.text("40"))
    assert "pending_load_draft" in user_data(harness)

    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash", "uz")))

    assert harness.conversation_state("staff_bottle_loaded") is None
    assert "pending_load_draft" not in user_data(harness)
    await harness.send(driver_updates.text("12"))
    assert not backend_calls(harness, "POST", SESSION_OPEN)


async def test_a_slip_at_the_ten_litre_load_asks_for_zero_or_more_and_zero_loads_none(monkeypatch):
    """A driver with no 10 L types "o" for "0" at the 10 L step. 0 is the right answer there, and the refusal says
    so. Told "a positive number", he typed 1: the trip loaded a 10 L he never had, and the close showed a false
    10 L discrepancy of 1."""
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", LOAD_SCREEN, lambda _call: load_screen(products=[TEN_LITRE]))
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session(units=[unit_line(loaded=0, on_board=0)]))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.text("40"))
    harness.telegram.reset()

    await harness.send(driver_updates.text("o"))

    assert harness.telegram.last_shown().text == "Send a whole number, 0 or more."
    assert harness.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT
    assert not backend_calls(harness, "POST", SESSION_OPEN)

    await harness.send(driver_updates.text("0"))
    assert [call.data for call in backend_calls(harness, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": 40, "units": [{"product_id": 3, "quantity": 0}]}
    ]


# ===========================================================================
# Returning bottles to the warehouse
# ===========================================================================


async def test_a_driver_closes_the_day_by_returning_bottles_and_the_discrepancy_is_spelled_out(monkeypatch):
    """The end-of-shift write, and the one the driver is charged against.

    ``bottles_returned_to_warehouse`` is what closes the session; the
    difference between it and what the truck should still hold is the
    discrepancy the driver pays for. It must reach the backend verbatim, and
    the driver must be shown the discrepancy in the same breath — finding out
    later, from an admin, is how disputes start.
    """
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    harness.backend.route(
        "POST",
        SESSION_CLOSE,
        lambda _call: open_session(
            status="closed", bottles_returned_to_warehouse=7, discrepancy=2
        ),
    )

    driver_updates, labels = await sign_in(harness)
    hub_buttons = await open_cash_hub(harness, driver_updates, labels)
    assert "staff_bottle_return_warehouse" in hub_buttons

    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))
    prompt = harness.telegram.last_shown().text
    assert prompt.endswith(_curated("staff.delivery.enter_bottles_returned_qty")), (
        "the return prompt is missing; the driver is looking at a session summary "
        f"with no question attached: {prompt!r}"
    )
    assert _curated("staff.delivery.bottles_on_truck_label") in prompt, (
        "the prompt must state what the truck is supposed to still hold, or the "
        "driver has nothing to check their own count against"
    )
    assert harness.conversation_state("staff_bottle_returned_wh") == BOTTLE_SESSION_RETURNED_QTY_INPUT

    harness.telegram.reset()
    await harness.send(driver_updates.text("7"))

    posts = backend_calls(harness, "POST", SESSION_CLOSE)
    assert len(posts) == 1
    assert posts[0].data == {"bottles_returned_to_warehouse": 7}

    expected = _curated("staff.delivery.bottle_session_closed").format(
        count=7,
        disc_line=_curated("staff.delivery.discrepancy_nonzero").format(discrepancy=2),
        ref="SES-2026",
    )
    assert harness.telegram.last_shown().text == expected
    assert harness.conversation_state("staff_bottle_returned_wh") is None


async def test_the_close_asks_both_returns_and_shows_both_discrepancies(monkeypatch):
    harness = await build_driver(monkeypatch, language="uz")
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(units=[unit_line(on_board=2)]))
    harness.backend.route("POST", SESSION_CLOSE, lambda _call: open_session(
        status="closed", bottles_returned_to_warehouse=37, discrepancy=-2,
        units=[unit_line(on_board=2, returned=0, discrepancy=2)],
    ))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels, "uz")

    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))
    assert harness.telegram.last_shown().text.endswith(_curated("staff.delivery.enter_bottles_returned_qty", "uz"))
    await harness.send(driver_updates.text("37"))
    assert harness.telegram.last_shown().text == "Omborga nechta 10 Litr suv qaytarganingizni kiriting:"
    assert not backend_calls(harness, "POST", SESSION_CLOSE), "the close was posted before the second number"
    assert harness.conversation_state("staff_bottle_returned_wh") == BOTTLE_SESSION_RETURNED_QTY_INPUT
    await harness.send(driver_updates.text("0"))

    assert [call.data for call in backend_calls(harness, "POST", SESSION_CLOSE)] == [
        {"bottles_returned_to_warehouse": 37, "units_returned": [{"product_id": 3, "quantity": 0}]}
    ]
    expected = _curated("staff.delivery.bottle_session_closed", "uz").format(
        count=37,
        disc_line=_curated("staff.delivery.discrepancy_nonzero", "uz").format(discrepancy=-2),
        ref="SES-2026",
    ) + "\n⚠️ 10 Litr suv: 2 ta hisoblanmagan"
    assert harness.telegram.last_shown().text == expected
    assert harness.conversation_state("staff_bottle_returned_wh") is None
    assert "pending_return_draft" not in user_data(harness)


async def test_a_unit_line_that_balances_reads_as_zero_and_a_close_missing_a_line_asks_again(monkeypatch):
    harness = await build_driver(monkeypatch, language="uz")
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(units=[unit_line(on_board=2)]))
    replies = iter([
        staff_backend_failure(
            "incomplete", 400, error_code="BOTTLE_SESSION_RETURN_INCOMPLETE", error_type="VALIDATION_ERROR"
        ),
        open_session(
            status="closed", bottles_returned_to_warehouse=5, discrepancy=0,
            units=[unit_line(on_board=2, returned=2, discrepancy=0)],
        ),
    ])
    harness.backend.route("POST", SESSION_CLOSE, lambda _call: next(replies))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels, "uz")

    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))
    harness.telegram.reset()
    await harness.send(driver_updates.text("5"))
    await harness.send(driver_updates.text("2"))
    texts = [shown.text for shown in harness.telegram.shown]
    assert texts[-2:] == [
        "❌ " + _curated("staff.error.api.bottle_session_return_incomplete", "uz"),
        _curated("staff.delivery.enter_bottles_returned_qty", "uz"),
    ]
    assert harness.conversation_state("staff_bottle_returned_wh") == BOTTLE_SESSION_RETURNED_QTY_INPUT

    await harness.send(driver_updates.text("5"))
    await harness.send(driver_updates.text("2"))
    assert [call.data for call in backend_calls(harness, "POST", SESSION_CLOSE)][1:] == [
        {"bottles_returned_to_warehouse": 5, "units_returned": [{"product_id": 3, "quantity": 2}]}
    ]
    assert harness.telegram.last_shown().text.endswith("\n✅ 10 Litr suv: farq 0")
    assert harness.conversation_state("staff_bottle_returned_wh") is None


async def test_a_trip_summary_that_fails_to_draw_still_asks_every_line_and_closes_with_all_of_them(monkeypatch):
    """The return screen draws the trip above the question, and the drawing can fail (here: a trip with no start
    time). The lines the close must answer are read before the drawing, so a failed drawing costs the summary,
    never a line: the driver is still asked the 10 L, and the one close carries it. Read after the drawing, they
    were lost with it: the close went without `units_returned`, the backend refused it as incomplete, and the
    driver typed the 18.9 L number twice."""
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(
        started_at=None, units=[unit_line(on_board=2)],
    ))
    harness.backend.route("POST", SESSION_CLOSE, lambda _call: open_session(
        status="closed", bottles_returned_to_warehouse=37, discrepancy=-2,
        units=[unit_line(on_board=2, returned=2, discrepancy=0)],
    ))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)

    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_bottles_returned_qty")
    await harness.send(driver_updates.text("37"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_unit_returned_qty").format(
        product="10 Litr suv"
    )
    assert not backend_calls(harness, "POST", SESSION_CLOSE), "the close was posted before the 10 L number"
    await harness.send(driver_updates.text("2"))

    assert [call.data for call in backend_calls(harness, "POST", SESSION_CLOSE)] == [
        {"bottles_returned_to_warehouse": 37, "units_returned": [{"product_id": 3, "quantity": 2}]}
    ]
    assert harness.conversation_state("staff_bottle_returned_wh") is None


async def test_the_trip_screen_shows_ten_litre_on_board_and_free(monkeypatch):
    harness = await build_driver(monkeypatch, language="uz")
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(
        units=[unit_line(on_board=8, free=5), unit_line(product_id=4, product_name="5 Litr <suv>", on_board=3, free=3)],
    ))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels, "uz")

    await harness.send(driver_updates.tap("staff_bottle_my_accountability"))

    lines = harness.telegram.last_shown().text.splitlines()
    assert "🧴 10 Litr suv: 8 · bo'sh 5" in lines
    assert "🧴 5 Litr &lt;suv&gt;: 3 · bo'sh 3" in lines


async def test_returning_zero_bottles_is_a_legitimate_answer_even_though_minus_one_is_not(monkeypatch):
    """Asymmetry that matters: loading needs > 0, returning allows 0.

    A driver who sold everything genuinely returns nothing, and refusing "0"
    would leave their session open overnight — which blocks the next morning's
    load and strands every order behind a BOTTLE_SESSION_REQUIRED error. But
    "-1" is still a typo and must never become a credit.
    """
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(current_inventory=0))
    harness.backend.route(
        "POST",
        SESSION_CLOSE,
        lambda _call: open_session(status="closed", bottles_returned_to_warehouse=0, discrepancy=0),
    )

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))

    harness.telegram.reset()
    await harness.send(driver_updates.text("-1"))
    assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP)
    assert harness.conversation_state("staff_bottle_returned_wh") == BOTTLE_SESSION_RETURNED_QTY_INPUT
    assert not backend_calls(harness, "POST", SESSION_CLOSE)

    harness.telegram.reset()
    await harness.send(driver_updates.text("0"))
    assert [call.data for call in backend_calls(harness, "POST", SESSION_CLOSE)] == [
        {"bottles_returned_to_warehouse": 0}
    ]
    assert _curated("staff.delivery.discrepancy_zero") in harness.telegram.last_shown().text
    assert harness.conversation_state("staff_bottle_returned_wh") is None


async def test_an_over_return_is_accepted_however_large_but_a_keypad_slip_is_not(monkeypatch):
    """The bound on this field is the COLUMN, and nothing else.

    ``receive_bottles_returned`` guarded ``count < 0`` only, so the same slip
    wave 1 caught on the load side — a phone number typed into the quantity box
    — sailed through into ``bottles_returned_to_warehouse``, a 4-byte
    PostgreSQL integer, and came back to the driver as a generic 500.

    The load-out ceiling is the WRONG fix here. Everything the truck left with
    plus every empty collected at a door comes back through this one field, so
    a big return is an ordinary day, not a typo: the bound must refuse nothing
    a driver could actually be holding. ``BOTTLE_RETURN_COLUMN_CEILING``
    therefore sits at the storage limit, not at a plausible one.
    """
    from staff_bot.handlers.delivery.bottle_collection import (
        BOTTLE_RETURN_COLUMN_CEILING,
        MAX_BOTTLES_PER_SESSION,
    )

    column_ceiling = 2**31 - 1
    assert MAX_BOTTLES_PER_SESSION < BOTTLE_RETURN_COLUMN_CEILING <= column_ceiling, (
        "the return bound must be a storage bound, not the truck load-out ceiling"
    )

    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    harness.backend.route(
        "POST",
        SESSION_CLOSE,
        lambda _call: open_session(status="closed", bottles_returned_to_warehouse=0, discrepancy=0),
    )

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))

    harness.telegram.reset()
    await harness.send(driver_updates.text("40000000000"))

    assert not backend_calls(harness, "POST", SESSION_CLOSE), (
        "a count the storage column cannot hold was posted anyway"
    )
    assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP), (
        "the driver was not told what was wrong with what they typed"
    )
    assert harness.conversation_state("staff_bottle_returned_wh") == BOTTLE_SESSION_RETURNED_QTY_INPUT, (
        "the refusal dropped the driver out of the prompt they still have to answer"
    )

    # An over-return far past anything the truck could have carried out is a
    # real end of shift, and must post unchanged.
    over_return = MAX_BOTTLES_PER_SESSION * 2
    await harness.send(driver_updates.text(str(over_return)))
    assert [call.data for call in backend_calls(harness, "POST", SESSION_CLOSE)] == [
        {"bottles_returned_to_warehouse": over_return}
    ], "a legitimate over-return was refused, or posted as something else"


async def test_a_driver_with_no_open_session_is_not_asked_for_a_return_count(driver):
    """Nothing to close. The bot must say so and stop.

    The dangerous version of this screen is the one that asks for a count
    anyway: the driver types "7", the close fails on the backend, and they have
    no idea whether their day was reconciled or not.
    """
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_return_warehouse"))

    assert driver.telegram.last_shown().text == _curated("staff.delivery.no_active_bottle_session")
    assert driver.conversation_state("staff_bottle_returned_wh") is None

    driver.telegram.reset()
    await driver.send(driver_updates.text("7"))
    assert not backend_calls(driver, "POST", SESSION_CLOSE)


async def test_a_backend_that_refuses_the_close_says_so_and_does_not_leave_the_count_armed(monkeypatch):
    """The backend is down at 19:00 and the driver is standing at the depot.

    ``session/close`` is a POST, and this client deliberately does NOT re-send
    POSTs after an ambiguous failure (``RETRY_SAFE_METHODS``) — a re-sent close
    is a duplicated inventory movement. So the driver must be told, and the
    flow must end cleanly: a flow left armed here turns their next message into
    a SECOND close attempt they never asked for.
    """
    harness = await build_driver(monkeypatch)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    harness.backend.route(
        "POST", SESSION_CLOSE, lambda _call: staff_backend_failure("boom", status_code=500)
    )
    errors = capture_errors(harness)

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_return_warehouse"))

    harness.telegram.reset()
    await harness.send(driver_updates.text("7"))

    assert errors == [], f"the failed close raised instead of explaining itself: {errors}"
    assert harness.telegram.last_shown().text == f"❌ {_curated('staff.error.api.service_unavailable')}"
    assert harness.conversation_state("staff_bottle_returned_wh") is None
    assert len(backend_calls(harness, "POST", SESSION_CLOSE)) == 1

    harness.telegram.reset()
    await harness.send(driver_updates.text("7"))
    assert len(backend_calls(harness, "POST", SESSION_CLOSE)) == 1, (
        "the driver's next message became a second close attempt"
    )


# The numbers the session screens show (spec 2026-11-09 §4.4, U1). Every value
# below is distinct, so a line that added two fields, or read the wrong one,
# shows a number the backend never published.
SPLIT_SESSION = dict(
    bottles_transferred_out=3, empties_transferred_out=2,
    bottles_transferred_in=1, empties_transferred_in=4,
    full_bottles_on_board=12, empties_on_board=7, free_full_bottles=6, current_inventory=19,
)


def truck_lines(*, full, empties, free_full) -> list:
    return [
        "💧 " + _curated("staff.bottles.full_on_truck").format(n=f"<b>{full}</b>"),
        "♻️ " + _curated("staff.bottles.empties_on_truck").format(n=f"<b>{empties}</b>"),
        "✅ " + _curated("staff.bottles.free_full_bottles").format(n=f"<b>{free_full}</b>"),
    ]


def owner_line(name_html: str) -> str:
    return f"👤 {_curated('staff.bottles.session_owner')}: <b>{name_html}</b>"


async def test_session_screen_shows_backend_numbers(driver):
    """Full on truck, empties on truck and free full bottles, each the field the
    backend publishes, and the transfers split into full and empties."""
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(**SPLIT_SESSION))
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))

    lines = driver.telegram.last_shown().text.splitlines()
    for expected in [
        "📤 " + _curated("staff.delivery.bottles_transferred_out_split").format(full="<b>3</b>", empty="<b>2</b>"),
        "📥 " + _curated("staff.delivery.bottles_transferred_in_split").format(full="<b>1</b>", empty="<b>4</b>"),
        *truck_lines(full=12, empties=7, free_full=6),
    ]:
        assert expected in lines, f"{expected!r} is not on the session screen: {lines}"


async def test_the_return_screen_states_the_total_the_backend_expects_back(driver):
    """The return prompt shows what the truck should still hold: the backend's own
    total (`current_inventory`, full plus empties), next to its two parts."""
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(**SPLIT_SESSION))
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_return_warehouse"))

    lines = driver.telegram.last_shown().text.splitlines()
    [total] = [line for line in lines if _curated("staff.delivery.bottles_on_truck_label") in line]
    assert total.endswith("<b>19</b>"), total
    for expected in truck_lines(full=12, empties=7, free_full=6):
        assert expected in lines, f"{expected!r} is not on the return screen: {lines}"
    assert lines[-1] == _curated("staff.delivery.enter_bottles_returned_qty")


# ===========================================================================
# Collecting bottles at a customer's door
# ===========================================================================


async def collection_backend(harness):
    harness.backend.route("GET", CUSTOMER_SEARCH, lambda _call: [
        {"id": CUSTOMER_ID, "first_name": "Kamola", "last_name": "Yusupova", "phone": "+998901234567"},
    ])
    harness.backend.route("GET", CUSTOMER_SUMMARY, lambda _call: BOTTLE_SUMMARY)
    harness.backend.route("GET", CUSTOMER_ADDRESSES, lambda _call: [PLACE_ROW])
    harness.backend.route("POST", COLLECTION, lambda _call: {"remaining_balance": 2})


async def walk_to_the_note_prompt(harness, driver_updates, labels):
    """Search → customer → place → quantity, leaving the note prompt on screen."""
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_collect_menu"))
    await harness.send(driver_updates.text("Kamola"))

    results = harness.telegram.last_shown()
    assert f"staff_bottle_customer_{CUSTOMER_ID}" in results.callback_data()

    await harness.send(driver_updates.tap(f"staff_bottle_customer_{CUSTOMER_ID}"))
    statement = harness.telegram.last_shown()
    assert f"staff_bottle_collect_{CUSTOMER_ID}_{ADDRESS_ID}" in statement.callback_data(), (
        f"the only place with bottles is not offered for collection: {statement.callback_data()}"
    )

    await harness.send(driver_updates.tap(f"staff_bottle_collect_{CUSTOMER_ID}_{ADDRESS_ID}"))
    picker = harness.telegram.last_shown()
    assert picker.text == _curated("staff.delivery.enter_bottle_collection_qty")
    assert f"staff_bottle_qty_{CUSTOMER_ID}_{ADDRESS_ID}_4" in picker.callback_data(), (
        "the 'collect all' shortcut is not capped at the place's balance"
    )

    await harness.send(driver_updates.tap(f"staff_bottle_qty_{CUSTOMER_ID}_{ADDRESS_ID}_2"))
    note_prompt = harness.telegram.last_shown()
    assert note_prompt.text == _curated("staff.delivery.enter_bottle_collection_note")
    # Load-bearing for every caller below: from here the collection is fully
    # armed and ONE text update away from the ledger. Without this check a
    # caller asserting "nothing was posted" could be passing simply because the
    # walk never got that far.
    armed = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID].get(
        "pending_bottle_collection_flow"
    )
    assert armed and armed.get("action") == "collect" and armed.get("quantity") == 2, (
        f"the note prompt is on screen but no collection is armed behind it: {armed}"
    )
    harness.telegram.reset()
    return note_prompt


async def test_a_driver_searches_a_customer_picks_a_place_and_the_collection_reaches_the_backend_once(driver):
    """The full at-the-door journey, six taps and one typed note.

    Every step of it is a different handler and half of them live OUTSIDE the
    conversation, so this is the only kind of test that can catch a step wired
    to a callback nobody registered. The POST body is asserted key by key
    because it IS the ledger row: a wrong ``address_id`` credits the empties to
    the wrong door, and a missing ``idempotency_key`` lets a replayed request
    debit the customer twice.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    await walk_to_the_note_prompt(driver, driver_updates, labels)

    searches = backend_calls(driver, "GET", CUSTOMER_SEARCH)
    assert len(searches) == 1
    assert searches[0].params == {
        "q": "Kamola",
        "type": detect_search_type("Kamola"),
        "only_with_open_cod": "false",
    }, (
        "bottle collection must search ALL customers — filtering by open COD hides "
        "every customer who has already paid but still holds empties"
    )

    await driver.send(driver_updates.text("left with the guard"))

    posts = backend_calls(driver, "POST", COLLECTION)
    assert len(posts) == 1, f"expected exactly one collection write, got {posts}"
    body = posts[0].data
    assert body["customer_id"] == CUSTOMER_ID
    assert body["address_id"] == ADDRESS_ID
    assert body["quantity"] == 2
    assert body["notes"] == "left with the guard"
    assert re.fullmatch(r"[0-9a-f]{32}", body["idempotency_key"]), (
        f"the per-intent retry token is missing or malformed: {body.get('idempotency_key')!r}"
    )

    assert driver.telegram.last_shown().text == _curated(
        "staff.delivery.bottle_collection_recorded"
    ).format(quantity=2, remaining=format_quantity(2))
    assert "pending_bottle_collection_flow" not in user_data(driver), (
        "the collection flow is still armed after a successful write"
    )


async def test_a_one_character_search_is_refused_without_a_round_trip_and_the_driver_can_retype(driver):
    """A driver at a door types one letter and waits.

    The backend refuses queries under two characters anyway, so sending it is
    a wasted round trip on a phone with one bar of signal. More importantly the
    prompt must survive the refusal: dropping out of the search here means
    re-navigating the whole cash hub while the customer stands there.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_collect_menu"))
    assert driver.telegram.last_shown().text == _curated(
        "staff.delivery.bottle_collection_search_prompt"
    )
    assert driver.conversation_state("staff_bottle_collection_search") == BOTTLE_COLLECTION_SEARCH_INPUT

    driver.telegram.reset()
    await driver.send(driver_updates.text("K"))
    assert driver.telegram.last_shown().text == _curated("staff.operator.search_too_short")
    assert not backend_calls(driver, "GET", CUSTOMER_SEARCH)
    assert driver.conversation_state("staff_bottle_collection_search") == BOTTLE_COLLECTION_SEARCH_INPUT

    await driver.send(driver_updates.text("Kamola"))
    assert len(backend_calls(driver, "GET", CUSTOMER_SEARCH)) == 1


async def test_a_menu_tap_while_the_note_prompt_is_open_abandons_the_collection_instead_of_saving_one(driver):
    """The riskiest tap in the whole bot.

    At the note prompt the collection is fully loaded — customer, place,
    quantity, retry token — and the ONLY thing standing between it and the
    ledger is the next text update. The note step is not inside a
    ConversationHandler at all; it is served by the global text router, which
    must recognise a main-menu label BEFORE it treats the text as a note.

    Get that order wrong and tapping "💰 Cash" writes a collection whose note
    reads "Cash", debits the customer two bottles they still hold, and mints a
    dispute the driver cannot explain.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    await walk_to_the_note_prompt(driver, driver_updates, labels)

    await driver.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

    assert not backend_calls(driver, "POST", COLLECTION), (
        "a main-menu tap was consumed as the note that finalised a real collection"
    )
    assert _curated("staff.cash.hub_title") in driver.telegram.last_shown().text
    assert "pending_bottle_collection_flow" not in user_data(driver), (
        "the loaded collection is still armed; the driver's next message writes it"
    )

    driver.telegram.reset()
    await driver.send(driver_updates.text("just checking"))
    assert not backend_calls(driver, "POST", COLLECTION)


async def test_the_quantity_picker_left_behind_by_a_menu_tap_answers_the_next_tap(driver):
    """The picker the menu tap disarmed is still on screen, and taps must land.

    The driver is looking at the quantity picker at a customer's door and taps
    "💰 Cash" on the permanently-visible reply keyboard — for a phone call, for
    a look at their float, for any reason at all. The cash hub opens and
    ``_clear_all_pending_flows`` pops ``pending_bottle_collection_flow``.

    The picker MESSAGE, though, is untouched: Telegram never removes it, so its
    quantity buttons stay in the scrollback looking exactly as live as they did
    a second ago. Tapping one used to reach ``pick_collection_qty``, fail its
    ``action != 'collect'`` guard, and answer with a BARE ``query.answer()`` —
    no ``text=``, so the spinner stopped and absolutely nothing was said. From
    the driver's side that is indistinguishable from a bot that has crashed, and
    what a driver does about it is tap again, harder.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_collect_menu"))
    await driver.send(driver_updates.text("Kamola"))
    await driver.send(driver_updates.tap(f"staff_bottle_customer_{CUSTOMER_ID}"))
    await driver.send(driver_updates.tap(f"staff_bottle_collect_{CUSTOMER_ID}_{ADDRESS_ID}"))
    picker = driver.telegram.last_shown()
    assert f"staff_bottle_qty_{CUSTOMER_ID}_{ADDRESS_ID}_2" in picker.callback_data()

    await driver.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))
    assert "pending_bottle_collection_flow" not in user_data(driver), (
        "fixture: the menu tap is what clears the flow under the open picker"
    )

    driver.telegram.reset()
    await driver.send(driver_updates.tap(f"staff_bottle_qty_{CUSTOMER_ID}_{ADDRESS_ID}_2"))

    answers = driver.telegram.of("answerCallbackQuery")
    assert answers and answers[-1].params.get("text") == _curated("staff.cancelled"), (
        f"the driver tapped a live-looking button and was told nothing: "
        f"{[a.params for a in answers]}"
    )
    cleaned = driver.telegram.last_shown()
    assert cleaned.callback_data() == ["staff_cash_hub"], (
        "the quantity buttons are still on screen, so the next tap is just as dead"
    )
    assert not backend_calls(driver, "POST", COLLECTION), (
        "the stale tap wrote a collection at a door the driver already left"
    )


async def test_a_backend_refusal_disarms_the_collection_so_the_next_message_is_not_a_silent_retry(driver):
    """The POST fails. What the driver types next must not be a second attempt.

    This flow used to clear itself only on SUCCESS, so after a failed write the
    flow still carried ``action='collect'`` plus a quantity, and the global text
    router finalised a collection nobody confirmed on the driver's very next
    message. A collection that did not land must cost one re-pick — never a
    phantom debit at a customer's door.
    """
    driver.backend.route("GET", CUSTOMER_SEARCH, lambda _call: [
        {"id": CUSTOMER_ID, "first_name": "Kamola", "last_name": "Yusupova", "phone": "+998901234567"},
    ])
    driver.backend.route("GET", CUSTOMER_SUMMARY, lambda _call: BOTTLE_SUMMARY)
    driver.backend.route("GET", CUSTOMER_ADDRESSES, lambda _call: [PLACE_ROW])
    driver.backend.route(
        "POST", COLLECTION, lambda _call: staff_backend_failure("boom", status_code=500)
    )
    errors = capture_errors(driver)

    driver_updates, labels = await sign_in(driver)
    await walk_to_the_note_prompt(driver, driver_updates, labels)

    await driver.send(driver_updates.text("handed over at the gate"))
    assert errors == [], f"the failed collection raised instead of explaining itself: {errors}"
    assert driver.telegram.last_shown().text == f"❌ {_curated('staff.error.api.service_unavailable')}"
    assert len(backend_calls(driver, "POST", COLLECTION)) == 1
    assert "pending_bottle_collection_flow" not in user_data(driver)

    driver.telegram.reset()
    await driver.send(driver_updates.text("anything at all"))
    assert len(backend_calls(driver, "POST", COLLECTION)) == 1, (
        "the driver's next message re-posted a collection they never re-confirmed"
    )


async def test_a_failed_balance_read_is_reported_not_shown_as_no_bottles(driver):
    """The statement screen reads the addresses once; the collect tap reads them
    again. When that second read fails, the driver used to be told the customer
    has no bottles — and walked away from a door that owed empties."""
    await collection_backend(driver)
    reads = {"count": 0}

    def _addresses(_call):
        reads["count"] += 1
        return [PLACE_ROW] if reads["count"] == 1 else staff_backend_failure("boom", 500)

    driver.backend.route("GET", CUSTOMER_ADDRESSES, _addresses)
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_collect_menu"))
    await driver.send(driver_updates.text("Kamola"))
    await driver.send(driver_updates.tap(f"staff_bottle_customer_{CUSTOMER_ID}"))
    driver.telegram.reset()

    await driver.send(driver_updates.tap(f"staff_bottle_collect_{CUSTOMER_ID}_{ADDRESS_ID}"))

    seen = [call.text for call in driver.telegram.shown] + [
        call.params.get("text", "") for call in driver.telegram.visible_answers
    ]
    assert f"❌ {_curated('staff.error.api.service_unavailable')}" in seen, seen
    assert not any(_curated("staff.delivery.no_bottle_balance") in text for text in seen)
    assert driver.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID].get(
        "pending_bottle_collection_flow"
    ) is None, "a collection stayed armed behind an error screen"


async def test_a_double_tap_on_save_without_note_records_the_collection_exactly_once(driver):
    """Phones in vans get tapped twice. Inventory must not.

    The second tap arrives on the same, still-visible message, so nothing about
    the update tells the bot it is a repeat. The flow dict is what makes it
    safe: it is cleared the moment the first write finishes, and the second tap
    finds nothing to submit. If that clear ever moves, a nervous double tap
    debits the customer four bottles instead of two.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    note_prompt = await walk_to_the_note_prompt(driver, driver_updates, labels)
    assert "staff_bottle_collect_save_no_note" in note_prompt.callback_data()

    await driver.send(driver_updates.tap("staff_bottle_collect_save_no_note"))
    await driver.send(driver_updates.tap("staff_bottle_collect_save_no_note"))

    posts = backend_calls(driver, "POST", COLLECTION)
    assert len(posts) == 1, f"a double tap wrote the collection {len(posts)} times"
    assert posts[0].data["notes"] == "", (
        "Save-without-note must post an empty note, not omit the field"
    )


async def test_a_stale_quantity_button_from_a_finished_collection_cannot_write_a_second_one(driver):
    """Old messages never disappear from a Telegram chat.

    The quantity picker the driver used ten minutes ago is still sitting in the
    scrollback with live buttons on it. Tapping one again must not re-arm a
    collection at a door the driver has long since left — the flow it belonged
    to is gone, and the tap has to die there.
    """
    await collection_backend(driver)
    driver_updates, labels = await sign_in(driver)
    await walk_to_the_note_prompt(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_collect_save_no_note"))
    assert len(backend_calls(driver, "POST", COLLECTION)) == 1

    driver.telegram.reset()
    await driver.send(driver_updates.tap(f"staff_bottle_qty_{CUSTOMER_ID}_{ADDRESS_ID}_4"))

    assert len(backend_calls(driver, "POST", COLLECTION)) == 1, (
        "a stale quantity button re-opened a finished collection"
    )
    assert "pending_bottle_collection_flow" not in user_data(driver)

    # Safe is not enough: this tap used to be answered with an EMPTY callback
    # answer and nothing else, so the spinner stopped and the driver — standing
    # at a door, looking at buttons that still look live — got no explanation
    # and tapped harder. It must say something, and the dead buttons must go.
    answers = driver.telegram.of("answerCallbackQuery")
    assert answers and answers[-1].params.get("text") == _curated("staff.cancelled"), (
        f"the stale tap was answered with no toast: {[a.params for a in answers]}"
    )
    picker_cleanup = driver.telegram.last_shown()
    assert picker_cleanup.text == _curated("staff.cancelled")
    assert picker_cleanup.callback_data() == ["staff_cash_hub"], (
        "the stale picker's quantity buttons are still on screen"
    )

    driver.telegram.reset()
    await driver.send(driver_updates.text("a note for the collection that is over"))
    assert len(backend_calls(driver, "POST", COLLECTION)) == 1


async def test_a_fine_for_zero_bottles_is_refused_as_not_positive(driver):
    """A fine is for one bottle or more, so 0 is refused at its quantity, in words a 0 does not satisfy. The
    count steps' "0 or more" sentence would refuse the 0 with a reason the 0 meets, and ask again forever."""
    driver_updates, _labels = await sign_in(driver)
    await driver.send(driver_updates.tap(f"staff_bottle_fine_{CUSTOMER_ID}_{ADDRESS_ID}"))
    assert driver.telegram.last_shown().text == _curated("staff.delivery.enter_fine_bottle_qty")
    driver.telegram.reset()

    await driver.send(driver_updates.text("0"))

    assert driver.telegram.last_shown().text == "Please enter a valid positive number."
    await driver.send(driver_updates.text("1"))
    assert driver.telegram.last_shown().text == _curated("staff.delivery.enter_fine_amount")
    assert not backend_calls(driver, "POST", "/api/v1/staff/bottles/fine")


# ===========================================================================
# Transferring bottles between drivers
# ===========================================================================
# A transfer moves full bottles AND empties (spec 2026-11-09 T1-T5). The
# backend publishes what the sender can hand over (`transferable_full`,
# `transferable_empties`), caps each number, and books each on its own counter.
# The bot only shows those numbers and posts what the driver chose: it caps
# nothing and adds nothing up.


def transfer_backend(harness, *, full=5, empties=2, drivers=None, transferable_any=True, units=()):
    """The sender's open trip holds `full` full bottles and `empties` empties.

    The send answers with the transfer the backend recorded, in the shape
    `serialize_bottle_transfer` publishes (to_dict's `declared_*_quantity` plus
    the `declared_full/empty` split).
    """
    # Load is how these tests reach the session menu: with a trip open, the load screen answers its own refusal.
    harness.backend.route("GET", LOAD_SCREEN, lambda _call: load_screen(
        refusal={
            "error_code": "BOTTLE_SESSION_ALREADY_OPEN", "owner_name": "Aziz", "session_id": 12,
            "bottles_on_board": full, "units": [],
        },
        open_session=open_session(),
    ))
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(
        full_bottles_on_board=full,
        empties_on_board=empties,
        transferable_full=full,
        transferable_empties=empties,
        current_inventory=full + empties,
        transferable_any=transferable_any,
        units=list(units),
    ))
    harness.backend.route(
        "GET",
        TRANSFER_RECIPIENTS,
        lambda _call: drivers if drivers is not None else [
            {"user_id": RECEIVER_DRIVER_ID, "name": "Bekzod Rahimov", "bottle_status": "own_session"},
        ],
    )
    harness.backend.route("POST", TRANSFERS, lambda _call: recorded_transfer(full=full, empty=empties))


def recorded_transfer(*, full, empty, units=(), **overrides) -> dict:
    """A PENDING transfer as the backend serializes it. `units` are (product_id, name, declared) one-way lines;
    `lines` is `transfer_lines`' shape: full, empty, then one line per unit."""
    transfer = {
        "lines": [
            {"kind": "full", "product_id": None, "product_name": None, "declared": full, "confirmed": None},
            {"kind": "empty", "product_id": None, "product_name": None, "declared": empty, "confirmed": None},
            *(
                {"kind": "product", "product_id": pid, "product_name": name, "declared": declared, "confirmed": None}
                for pid, name, declared in units
            ),
        ],
        "id": TRANSFER_ID,
        "transfer_ref": "TRF-31-2026",
        "status": "pending",
        "declared_quantity": full,
        "declared_empty_quantity": empty,
        "declared_full": full,
        "declared_empty": empty,
        "declared_total": full + empty,
        "confirmed_quantity": None,
        "confirmed_empty_quantity": None,
        "confirmed_full": None,
        "confirmed_empty": None,
        "confirmed_total": None,
        "sender_name": "Bekzod Rahimov",
    }
    transfer.update(overrides)
    return transfer


def counted_transfer(*, declared, confirmed, status, admins_alerted, transfer_id=TRANSFER_ID, units=()) -> dict:
    """The confirm route's answer: the counted transfer, each line with what was declared and counted, plus
    `admins_alerted`. `units` are (product_id, name, declared, confirmed) one-way lines."""
    (dfull, dempty), (cfull, cempty) = declared, confirmed
    transfer = recorded_transfer(
        full=dfull, empty=dempty,
        units=[(product_id, name, sent) for product_id, name, sent, _counted in units],
        id=transfer_id,
        status=status,
        confirmed_quantity=cfull,
        confirmed_empty_quantity=cempty,
        confirmed_full=cfull,
        confirmed_empty=cempty,
        confirmed_total=cfull + cempty,
    )
    for line, counted in zip(transfer["lines"], (cfull, cempty, *(c for *_sent, c in units))):
        line["confirmed"] = counted
    return {**transfer, "admins_alerted": admins_alerted}


# Aziz's handover in the spec's example: 10 full, 4 empties and 6 x 10 L.
TEN_LITRE_SENT = (3, "10 Litr suv", 6)


def rejected_transfer(*, sender_name="Aziz", sender_trip_was_closed=False) -> dict:
    """The reject route's answer: the transfer, now `rejected`, plus `sender_trip_was_closed`."""
    return {
        **recorded_transfer(
            full=10, empty=4, units=[TEN_LITRE_SENT],
            status="rejected", rejected_at="2026-11-24T09:00:00+00:00", sender_name=sender_name,
        ),
        "sender_trip_was_closed": sender_trip_was_closed,
    }


def pending_inbox(harness, *rows):
    harness.backend.route("GET", TRANSFERS_PENDING, lambda _call: [dict(row) for row in rows])


async def start_transfer(harness, driver_updates, labels):
    """Cash hub → session menu → transfer → colleague, ending on the send screen."""
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    session_menu = harness.telegram.last_shown()
    assert "staff_bottle_transfer_start" in session_menu.callback_data(), (
        f"the session screen offers no transfer button: {session_menu.callback_data()}"
    )

    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))
    picker = harness.telegram.last_shown()
    assert f"staff_transfer_driver_{RECEIVER_DRIVER_ID}" in picker.callback_data()
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_DRIVER_SELECT

    harness.telegram.reset()
    await harness.send(driver_updates.tap(f"staff_transfer_driver_{RECEIVER_DRIVER_ID}"))
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_SEND_CHOICE
    send_screen = harness.telegram.last_shown()
    harness.telegram.reset()
    return send_screen


async def choose_amounts(harness, driver_updates, labels):
    """The send screen's "Choose amounts", ending on the full-bottles prompt."""
    await start_transfer(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_transfer_choose"))
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_FULL_QTY_INPUT
    harness.telegram.reset()


async def test_the_send_screen_shows_the_backends_transferable_numbers_and_both_ways_to_send(monkeypatch):
    """After picking the colleague, the sender sees what the backend says he can
    hand over, and two buttons: everything, or his own two numbers. The numbers
    on the screen and in the button are `transferable_full` / `transferable_empties`
    as published, never a total the bot made up."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    driver_updates, labels = await sign_in(harness)

    send_screen = await start_transfer(harness, driver_updates, labels)

    assert send_screen.text.splitlines() == [
        _curated("staff.delivery.transfer_send_prompt"),
        "💧 " + _curated("staff.bottles.full_on_truck").format(n="<b>5</b>"),
        "♻️ " + _curated("staff.bottles.empties_on_truck").format(n="<b>2</b>"),
    ]
    assert send_screen.button_labels()[:2] == ["📦 Send all (18.9 L full 5 · empty 2)", "Choose amounts"]
    assert send_screen.callback_data() == ["staff_transfer_send_all", "staff_transfer_choose", "staff_cash_hub"]

    # Choose amounts asks full first, then empties, each with the backend's maximum.
    await harness.send(driver_updates.tap("staff_transfer_choose"))
    assert harness.telegram.last_shown().text == "How many full bottles? (max 5)"
    await harness.send(driver_updates.text("5"))
    assert harness.telegram.last_shown().text == "How many empty bottles? (max 2)"
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_EMPTY_QTY_INPUT
    assert not backend_calls(harness, "POST", TRANSFERS), "a transfer was sent before the empties were asked"


async def test_send_all_posts_send_all_and_the_receipt_names_what_the_backend_recorded(monkeypatch):
    """"Send all" sends whatever is on the truck when the backend books it (T3).
    The bot posts the flag, not two numbers of its own, and the receipt names
    what the backend recorded. Here a stop was delivered between the screen and
    the tap, so the backend booked 4 full, not the 5 the button showed."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    harness.backend.route("POST", TRANSFERS, lambda _call: recorded_transfer(full=4, empty=2))
    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)

    await harness.send(driver_updates.tap("staff_transfer_send_all"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "send_all": True}
    ]
    receipt = harness.telegram.last_shown()
    assert receipt.text == _curated("staff.delivery.bottle_transfer_sent_lines").format(
        lines="18.9 L full 4 · empty 2", ref="TRF-31-2"
    )
    assert "staff_bottle_transfers_pending" in receipt.callback_data()
    assert harness.conversation_state("staff_bottle_transfer") is None
    for key in ("pending_transfer_flow", "pending_transfer_receiver_id", "pending_transfer_full_qty"):
        assert key not in user_data(harness), f"{key} outlived the transfer it belonged to"

    # The send screen is still in the chat. Its button cannot send a second batch.
    harness.telegram.reset()
    await harness.send(driver_updates.tap("staff_transfer_send_all"))
    assert harness.telegram.last_shown().text == _curated("staff.cancelled")
    assert len(backend_calls(harness, "POST", TRANSFERS)) == 1


async def test_choose_amounts_posts_both_numbers_the_driver_typed(monkeypatch):
    """Five full and two empties change trucks. Both numbers reach the backend
    as typed, on their own fields, with the colleague picked on the screen
    before. Keypad slips are refused on the spot and the prompt stays open;
    zero full is a real answer (a driver handing over only empties)."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    for slip in ("five", "-1", "2.5"):
        harness.telegram.reset()
        await harness.send(driver_updates.text(slip))
        assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP), slip
        assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_FULL_QTY_INPUT, slip

    await harness.send(driver_updates.text("5"))
    assert user_data(harness)["pending_transfer_full_qty"] == 5

    harness.telegram.reset()
    await harness.send(driver_updates.text("two"))
    assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP)
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_EMPTY_QTY_INPUT
    assert not backend_calls(harness, "POST", TRANSFERS)

    await harness.send(driver_updates.text("2"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 5, "empty_quantity": 2}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.bottle_transfer_sent_lines").format(
        lines="18.9 L full 5 · empty 2", ref="TRF-31-2"
    )
    assert harness.conversation_state("staff_bottle_transfer") is None

    # Walking away and typing the same number again must not hand over a second batch.
    await harness.send(driver_updates.text("2"))
    assert len(backend_calls(harness, "POST", TRANSFERS)) == 1


async def test_only_empties_can_be_sent(monkeypatch):
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    await harness.send(driver_updates.text("0"))
    await harness.send(driver_updates.text("2"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 0, "empty_quantity": 2}
    ]


def send_screen_lines(*, full, empties, units=()) -> list:
    """The send screen's text below any refusal: the prompt, the backend's two numbers, one line per unit."""
    return [
        _curated("staff.delivery.transfer_send_prompt"),
        "💧 " + _curated("staff.bottles.full_on_truck").format(n=f"<b>{full}</b>"),
        "♻️ " + _curated("staff.bottles.empties_on_truck").format(n=f"<b>{empties}</b>"),
        *(f"🧴 {name}: {n}" for name, n in units),
    ]


def assert_back_on_the_send_screen(harness, *, refusal, full, empties):
    """A refused amount keeps the colleague and redraws the send screen under the refusal,
    with the numbers the truck holds now."""
    screen = harness.telegram.last_shown()
    assert screen.text.splitlines() == [refusal, "", *send_screen_lines(full=full, empties=empties)]
    assert screen.button_labels()[:2] == [f"📦 Send all (18.9 L full {full} · empty {empties})", "Choose amounts"]
    assert screen.callback_data() == ["staff_transfer_send_all", "staff_transfer_choose", "staff_cash_hub"]
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_SEND_CHOICE
    assert user_data(harness)["pending_transfer_receiver_id"] == RECEIVER_DRIVER_ID
    assert "pending_transfer_full_qty" not in user_data(harness), "the refused full count is still armed"


@pytest.mark.parametrize(
    "typed, details, sentence",
    [
        (("6", "2"), {"kind": "full", "requested": 6, "available": 4}, "❌ You have only 4 full bottles."),
        (("4", "3"), {"kind": "empty", "requested": 3, "available": 2}, "❌ You have only 2 empty bottles."),
    ],
    ids=["full-over", "empties-over"],
)
async def test_a_number_above_the_truck_goes_to_the_backend_and_its_refusal_names_the_kind(
    monkeypatch, typed, details, sentence
):
    """The bot no longer caps the numbers: the backend does, per kind (T2), and
    its refusal says which number was too high.

    The refusal is not the end of the handover. A co-driver's delivery took a
    full bottle off the truck while the driver typed (5 became 4), so the bot
    reads the session again and redraws the send screen with the numbers on the
    truck now, under the refusal. The colleague stays picked, and fixing one
    digit is a tap and two numbers, not the whole walk from the Cash menu."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        f"Cannot transfer {details['requested']} {details['kind']} bottle(s); the sender has "
        f"{details['available']} on the truck.", 400,
        error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
        details={**details, "validation_errors": []},
        error_type="VALIDATION_ERROR",
    ))
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(
        full_bottles_on_board=4, empties_on_board=2, transferable_full=4, transferable_empties=2,
    ))

    for number in typed:
        await harness.send(driver_updates.text(number))

    full, empty = (int(number) for number in typed)
    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": full, "empty_quantity": empty}
    ]
    assert_back_on_the_send_screen(harness, refusal=sentence, full=4, empties=2)

    # The way back: Choose amounts again, asked with the new maximum, and the corrected pair goes out.
    harness.backend.route("POST", TRANSFERS, lambda _call: recorded_transfer(full=4, empty=1))
    await harness.send(driver_updates.tap("staff_transfer_choose"))
    assert harness.telegram.last_shown().text == "How many full bottles? (max 4)"
    await harness.send(driver_updates.text("4"))
    await harness.send(driver_updates.text("1"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)][1:] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 4, "empty_quantity": 1}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.bottle_transfer_sent_lines").format(
        lines="18.9 L full 4 · empty 1", ref="TRF-31-2"
    )
    assert harness.conversation_state("staff_bottle_transfer") is None


async def test_an_older_backends_refusal_without_a_kind_still_names_both_numbers(monkeypatch):
    """During a restart the backend may still be the old one: its refusal names
    no kind. The bot falls back to the sentence with both numbers."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        "Cannot transfer 4 bottle(s); sender only has 2 on truck.", 400,
        error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
        details={"requested": 4, "available": 2, "validation_errors": []},
        error_type="VALIDATION_ERROR",
    ))
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    await harness.send(driver_updates.text("4"))
    await harness.send(driver_updates.text("0"))

    assert_back_on_the_send_screen(
        harness,
        refusal="❌ " + _curated("staff.error.api.transfer_exceeds_inventory_detail").format(available=2, requested=4),
        full=5, empties=2,
    )


async def test_nothing_to_send_is_the_backends_answer(monkeypatch):
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        "Nothing to transfer: no full bottles and no empties.", 400,
        error_code="BOTTLE_TRANSFER_NOTHING_TO_SEND", error_type="VALIDATION_ERROR",
    ))
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    await harness.send(driver_updates.text("0"))
    await harness.send(driver_updates.text("0"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 0, "empty_quantity": 0}
    ]
    assert _curated("staff.error.api.transfer_nothing_to_send") == (
        "Nothing to transfer. Choose at least one bottle to send."
    )
    assert_back_on_the_send_screen(
        harness, refusal="❌ " + _curated("staff.error.api.transfer_nothing_to_send"), full=5, empties=2,
    )


@pytest.mark.parametrize(
    "session_now",
    [
        staff_backend_failure("Service temporarily unavailable", 503),
        open_session(
            full_bottles_on_board=0, empties_on_board=0, transferable_full=0, transferable_empties=0,
            transferable_any=False, units=[],
        ),
    ],
    ids=["session-read-failed", "truck-now-empty"],
)
async def test_a_refusal_with_no_send_screen_to_redraw_still_leads_back_to_the_transfer(monkeypatch, session_now):
    """When the session cannot be read again, or nothing is left to send, there is
    no send screen to draw. The refusal still ends on a button back to the
    transfer start, never on a bare sentence."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        "Cannot transfer 6 full bottle(s); the sender has 5 on the truck.", 400,
        error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
        details={"kind": "full", "requested": 6, "available": 5, "validation_errors": []},
        error_type="VALIDATION_ERROR",
    ))
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: session_now)

    await harness.send(driver_updates.text("6"))
    await harness.send(driver_updates.text("2"))

    screen = harness.telegram.last_shown()
    assert screen.text == "❌ You have only 5 full bottles."
    assert list(zip(screen.button_labels(), screen.callback_data())) == [
        ("🔄 " + _curated("staff.menu.transfer_bottles_to_driver"), "staff_bottle_transfer_start"),
    ]
    assert harness.conversation_state("staff_bottle_transfer") is None
    for key in ("pending_transfer_flow", "pending_transfer_receiver_id", "pending_transfer_full_qty"):
        assert key not in user_data(harness), f"{key} outlived the transfer it belonged to"


async def test_a_driver_with_an_empty_truck_is_told_there_is_nothing_to_transfer_and_no_prompt_is_left_open(monkeypatch):
    """Nothing on board. The flow must refuse and close.

    A refusal that leaves the quantity state armed is worse than useless: the
    driver reads "no bottles to transfer", types something unrelated, and the
    bot answers with a bottle-count error about a transfer they abandoned.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=0, empties=0, transferable_any=False)
    driver_updates, labels = await sign_in(harness)

    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))

    assert harness.telegram.last_shown().text == _curated("staff.delivery.no_bottles_to_transfer")
    assert harness.conversation_state("staff_bottle_transfer") is None
    assert not backend_calls(harness, "GET", TRANSFER_RECIPIENTS), (
        "the driver list was fetched for a transfer that can never happen"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.text("4"))
    assert not backend_calls(harness, "POST", TRANSFERS)


async def test_a_truck_with_only_empties_can_still_hand_them_over(monkeypatch):
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=0, empties=3)
    driver_updates, labels = await sign_in(harness)

    send_screen = await start_transfer(harness, driver_updates, labels)

    assert send_screen.button_labels()[0] == "📦 Send all (18.9 L full 0 · empty 3)"


TEN_LITRE_TRIP_UNITS = [unit_line(on_board=6, transferable=6, free=6)]


async def test_choose_amounts_asks_full_empties_then_ten_litre_and_sends_once(monkeypatch):
    """A trip with a 10 L line: Choose amounts asks full, empties, then one prompt per unit line, each with the
    line's published `transferable` as its maximum. Nothing is posted until the last answer, and the send
    carries the unit line; the receipt prints every line the backend recorded."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=10, empties=4, units=TEN_LITRE_TRIP_UNITS)
    harness.backend.route("POST", TRANSFERS, lambda _call: recorded_transfer(
        full=10, empty=4, units=[(3, "10 Litr suv", 6)],
    ))
    driver_updates, labels = await sign_in(harness)
    send_screen = await start_transfer(harness, driver_updates, labels)
    assert send_screen.text.splitlines() == send_screen_lines(full=10, empties=4, units=[("10 Litr suv", 6)])
    assert send_screen.button_labels()[0] == "📦 Send all (18.9 L full 10 · empty 4 · 10 Litr suv 6)"

    await harness.send(driver_updates.tap("staff_transfer_choose"))
    await harness.send(driver_updates.text("10"))
    await harness.send(driver_updates.text("4"))

    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_UNIT_QTY_INPUT
    assert harness.telegram.last_shown().text == "How many 10 Litr suv? (max 6)"
    assert not backend_calls(harness, "POST", TRANSFERS), "a transfer was sent before the 10 L count was asked"

    await harness.send(driver_updates.text("6"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {
            "receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 10, "empty_quantity": 4,
            "units": [{"product_id": 3, "quantity": 6}],
        }
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.bottle_transfer_sent_lines").format(
        lines="18.9 L full 10 · empty 4 · 10 Litr suv 6", ref="TRF-31-2"
    )
    assert harness.conversation_state("staff_bottle_transfer") is None
    for key in ("pending_transfer_flow", "pending_transfer_receiver_id", "pending_transfer_full_qty",
                "pending_transfer_empty_qty", "pending_transfer_units"):
        assert key not in user_data(harness), f"{key} outlived the transfer it belonged to"


async def test_zero_at_the_ten_litre_prompt_sends_the_bottles_and_no_unit_line(monkeypatch):
    """The common send: a driver hands over 18.9 L and keeps his 10 L for his own stops, so he answers 0 at the
    10 L prompt. The send carries both 18.9 L numbers and no `units` key at all: a declared-0 line would be a
    line the receiver has to count."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=10, empties=4, units=TEN_LITRE_TRIP_UNITS)
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    await harness.send(driver_updates.text("10"))
    await harness.send(driver_updates.text("4"))
    assert harness.telegram.last_shown().text == "How many 10 Litr suv? (max 6)"
    await harness.send(driver_updates.text("0"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 10, "empty_quantity": 4}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.bottle_transfer_sent_lines").format(
        lines="18.9 L full 10 · empty 4", ref="TRF-31-2"
    )
    assert harness.conversation_state("staff_bottle_transfer") is None
    for key in ("pending_transfer_flow", "pending_transfer_receiver_id", "pending_transfer_full_qty",
                "pending_transfer_empty_qty", "pending_transfer_units"):
        assert key not in user_data(harness), f"{key} outlived the transfer it belonged to"


async def test_the_give_entry_opens_for_a_ten_litre_only_driver(monkeypatch):
    """No full bottle, no empty, only 10 L: the backend's `transferable_any` is True, so Give opens the picker
    and then the send screen, never "nothing to send"."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=0, empties=0, transferable_any=True, units=TEN_LITRE_TRIP_UNITS)
    driver_updates, labels = await sign_in(harness)

    send_screen = await start_transfer(harness, driver_updates, labels)

    assert send_screen.text.splitlines() == send_screen_lines(full=0, empties=0, units=[("10 Litr suv", 6)])
    assert send_screen.button_labels()[0] == "📦 Send all (18.9 L full 0 · empty 0 · 10 Litr suv 6)"
    assert _curated("staff.delivery.no_bottles_to_transfer") not in send_screen.text


async def test_an_over_cap_ten_litre_send_is_refused_by_name_and_the_send_screen_returns(monkeypatch):
    """Seven 10 L typed against six on the trip: the backend refuses by product, the bot names it above the send
    screen (not the end screen, not "no bottles"), keeps the colleague and drops the typed counts."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=0, empties=0, transferable_any=True, units=TEN_LITRE_TRIP_UNITS)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        "Cannot transfer 7 of 10 Litr suv; the sender has 6.", 400,
        error_code="BOTTLE_TRANSFER_EXCEEDS_INVENTORY",
        details={
            "kind": "product", "product_id": 3, "product_name": "10 Litr suv", "requested": 7, "available": 6,
            "validation_errors": [],
        },
        error_type="VALIDATION_ERROR",
    ))
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)

    for number in ("0", "0", "7"):
        await harness.send(driver_updates.text(number))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {
            "receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 0, "empty_quantity": 0,
            "units": [{"product_id": 3, "quantity": 7}],
        }
    ]
    screen = harness.telegram.last_shown()
    refusal = "❌ " + _curated("staff.error.api.transfer_exceeds_unit_detail").format(
        product="10 Litr suv", available=6,
    )
    assert screen.text.splitlines() == [
        refusal, "", *send_screen_lines(full=0, empties=0, units=[("10 Litr suv", 6)]),
    ]
    assert screen.callback_data() == ["staff_transfer_send_all", "staff_transfer_choose", "staff_cash_hub"]
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_SEND_CHOICE
    assert _curated("staff.delivery.no_bottles_to_transfer") not in screen.text
    assert user_data(harness)["pending_transfer_receiver_id"] == RECEIVER_DRIVER_ID
    for key in ("pending_transfer_full_qty", "pending_transfer_empty_qty", "pending_transfer_units"):
        assert key not in user_data(harness), f"the refused count {key} is still armed"


async def test_a_menu_tap_while_the_transfer_quantity_is_being_typed_cancels_the_handover(monkeypatch):
    """The other driver drives off mid-conversation and the sender gives up.

    Same state-leak class as the load flow, with worse consequences: a stale
    transfer prompt turns the next number the driver types anywhere in the bot
    into a handover of that many bottles to a colleague they picked minutes
    ago. Both prompts, full and empties.
    """
    for typed_before_leaving in ((), ("5",)):
        harness = await build_driver(monkeypatch)
        transfer_backend(harness)
        driver_updates, labels = await sign_in(harness)
        await choose_amounts(harness, driver_updates, labels)
        for number in typed_before_leaving:
            await harness.send(driver_updates.text(number))

        await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

        assert harness.conversation_state("staff_bottle_transfer") is None, (
            "the transfer quantity prompt is still armed after the driver navigated away"
        )
        assert _curated("staff.cash.hub_title") in harness.telegram.last_shown().text
        assert "pending_transfer_full_qty" not in user_data(harness)

        harness.telegram.reset()
        await harness.send(driver_updates.text("4"))
        assert not backend_calls(harness, "POST", TRANSFERS), (
            "a number typed after leaving the flow handed bottles to another driver"
        )


async def test_walking_away_from_the_driver_picker_disarms_the_stale_send_buttons(monkeypatch):
    """Was: ``test_walking_away_from_the_driver_picker_disarms_the_stale_truck_count``.

    ``pending_transfer_flow`` is what keeps a transfer alive. It is stamped when
    the flow opens and registered with the flow-state SSOT, so walking away takes
    it with it, and a tap on the picker or the send screen left in the chat is
    refused out loud instead of handing over bottles that have since left the
    truck.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    driver_updates, labels = await sign_in(harness)

    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_DRIVER_SELECT
    assert "pending_transfer_flow" in user_data(harness), "fixture: opening the picker arms the flow"

    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))
    assert "pending_transfer_flow" not in user_data(harness), (
        "the transfer survived the driver walking away"
    )

    # The afternoon happens: the truck is empty now.
    transfer_backend(harness, full=0, empties=0)

    for stale in (f"staff_transfer_driver_{RECEIVER_DRIVER_ID}", "staff_transfer_send_all", "staff_transfer_choose"):
        harness.telegram.reset()
        await harness.send(driver_updates.tap(stale))
        stale_tap = harness.telegram.last_shown()
        assert stale_tap.text == _curated("staff.cancelled"), (
            f"a tap on the abandoned {stale} must say the transfer is over: {stale_tap.text!r}"
        )
        assert stale_tap.callback_data(), (
            "the driver was left on a dead-end message with no way back to the session menu"
        )
        assert harness.conversation_state("staff_bottle_transfer") is None, (
            f"the stale {stale} tap re-armed the abandoned transfer"
        )

    await harness.send(driver_updates.text("9"))
    assert not backend_calls(harness, "POST", TRANSFERS), (
        "bottles that are no longer on the truck were handed over"
    )


async def test_a_transfer_the_other_driver_never_confirms_credits_nobody_and_is_still_waiting_in_their_inbox(monkeypatch):
    """The handover the receiver forgets about — the normal failure, not a rare one.

    A transfer is two writes: the sender declares it, the receiver confirms it.
    Between them the bottles belong to nobody, and the sender is still
    accountable. Nothing in the bot may quietly close that gap: no
    auto-confirm, no second POST, and the transfer must still be sitting in the
    receiver's inbox with both answers available — accept the declared counts,
    or say what actually arrived.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    pending_inbox(harness, recorded_transfer(full=5, empty=2, sender_name="Aziz Karimov"))

    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_transfer_send_all"))

    initiated = harness.telegram.last_shown()
    assert "staff_bottle_transfers_pending" in initiated.callback_data(), (
        "after declaring a transfer the driver has no route to the pending inbox"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))

    inbox = harness.telegram.last_shown()
    assert _curated("staff.delivery.pending_transfers_title") in inbox.text
    assert "📥 Aziz Karimov sent you: 18.9 L full 5 · empty 2  [ref: TRF-31-2]" in inbox.text
    buttons = list(zip(inbox.button_labels(), inbox.callback_data()))
    assert buttons[:3] == [
        ("✅ Confirm 18.9 L full 5 · empty 2", f"staff_transfer_confirm_{TRANSFER_ID}"),
        ("✏️ Enter other numbers", f"staff_transfer_custom_{TRANSFER_ID}"),
        ("❌ Reject", f"staff_transfer_reject_{TRANSFER_ID}"),
    ], "the receiver can only rubber-stamp the declared counts, or cannot even do that"

    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), (
        "an unconfirmed transfer was confirmed by merely looking at it"
    )
    assert not backend_calls(harness, "POST", TRANSFER_REJECT)
    assert len(backend_calls(harness, "POST", TRANSFERS)) == 1


# The spec's receiving screen, in the uz copy the seed ships (§6.7):
#   📥 Aziz sizga yubordi: 18,9 L to'la 10 · bo'sh 4 · 10 Litr suv 6
#   [✅ Tasdiqlash]  [✏️ Boshqa son kiritish]  [❌ Rad etish]
AZIZ_SENT_UZ = "📥 Aziz sizga yubordi: 18,9 L to'la 10 · bo'sh 4 · 10 Litr suv 6"
THREE_BUTTONS_UZ = [
    ("✅ Tasdiqlash: 18,9 L to'la 10 · bo'sh 4 · 10 Litr suv 6", f"staff_transfer_confirm_{TRANSFER_ID}"),
    ("✏️ Boshqa son kiritish", f"staff_transfer_custom_{TRANSFER_ID}"),
    ("❌ Rad etish", f"staff_transfer_reject_{TRANSFER_ID}"),
]


def azizs_handover(**overrides) -> dict:
    """Aziz's pending handover: 10 full, 4 empties and 6 x 10 L, every line as the backend publishes it."""
    return recorded_transfer(full=10, empty=4, units=[TEN_LITRE_SENT], sender_name="Aziz", **overrides)


async def open_inbox(harness, driver_updates):
    """📥 Incoming transfers, tapped from the session menu (its handler is global)."""
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))
    inbox = harness.telegram.last_shown()
    harness.telegram.reset()
    return inbox


async def test_the_inbox_and_push_show_every_line_and_three_buttons(monkeypatch):
    """The receiver reads who sent what, line by line, in the inbox and in the push alike, and gets three
    answers under each: Confirm (its label names every line), a count of his own, and Reject."""
    from staff_bot.webhook_server import _render_bottle_event

    harness = await build_driver(monkeypatch, language="uz")
    pending_inbox(harness, azizs_handover())
    driver_updates, _labels = await sign_in(harness)

    inbox = await open_inbox(harness, driver_updates)

    assert f"{AZIZ_SENT_UZ}  [ref: TRF-31-2]" in inbox.text
    assert list(zip(inbox.button_labels(), inbox.callback_data()))[:3] == THREE_BUTTONS_UZ

    push = {
        "id": TRANSFER_ID, "declared_quantity": 10, "declared_empty_quantity": 4, "sender_name": "Aziz",
        "lines": azizs_handover()["lines"],
    }
    text, keyboard = _render_bottle_event("transfer_received", push, "uz")

    assert text == AZIZ_SENT_UZ
    assert [(button.text, button.callback_data) for row in keyboard.inline_keyboard for button in row][:3] == (
        THREE_BUTTONS_UZ
    )
    assert not [call for call in harness.backend.calls if call.method == "POST" and "/transfers/" in call.endpoint]


async def test_reject_asks_once_then_rejects(monkeypatch):
    """Reject cannot be undone, so it asks one question, naming whose trip the bottles go back to. Yes posts
    one empty-bodied reject; the reply says the sender was told. No reason is asked."""
    harness = await build_driver(monkeypatch, language="uz")
    pending_inbox(harness, azizs_handover())
    harness.backend.route("POST", TRANSFER_REJECT, lambda _call: rejected_transfer())
    driver_updates, _labels = await sign_in(harness)
    await open_inbox(harness, driver_updates)

    await harness.send(driver_updates.tap(f"staff_transfer_reject_{TRANSFER_ID}"))

    question = harness.telegram.last_shown()
    assert question.text == "Rad etasizmi? Idishlar Azizning reysiga qaytadi."
    assert question.reply_markup["inline_keyboard"] == [[
        {"text": "Ha, rad etaman", "callback_data": f"staff_transfer_reject_yes_{TRANSFER_ID}"},
        {"text": "⬅️ Orqaga", "callback_data": "staff_bottle_transfers_pending"},
    ]]
    assert not backend_calls(harness, "POST", TRANSFER_REJECT), "the first tap rejected without asking"

    harness.telegram.reset()
    await harness.send(driver_updates.tap(f"staff_transfer_reject_yes_{TRANSFER_ID}"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_REJECT)] == [None]
    reply = harness.telegram.last_shown()
    assert reply.text == "❌ Rad etildi. Aziz xabardor qilindi."
    assert "staff_bottle_transfers_pending" in reply.callback_data()
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)


async def test_back_from_the_reject_question_leaves_it_pending(monkeypatch):
    """Back answers nothing: no reject, no count, and the inbox is drawn again with the transfer still in it."""
    harness = await build_driver(monkeypatch, language="uz")
    pending_inbox(harness, azizs_handover())
    driver_updates, _labels = await sign_in(harness)
    await open_inbox(harness, driver_updates)
    await harness.send(driver_updates.tap(f"staff_transfer_reject_{TRANSFER_ID}"))
    harness.telegram.reset()

    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))

    inbox = harness.telegram.last_shown()
    assert AZIZ_SENT_UZ in inbox.text
    assert list(zip(inbox.button_labels(), inbox.callback_data()))[:3] == THREE_BUTTONS_UZ
    assert not backend_calls(harness, "POST", TRANSFER_REJECT)
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)


async def test_rejecting_the_transfer_being_counted_drops_its_count(monkeypatch):
    """The receiver began "Enter other numbers" on Aziz's handover, typed the full bottles, then rejected it. His
    count is of a transfer that is settled now: the next number he types says the step is over and posts no count
    (the backend would refuse it as already handled)."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, azizs_handover())
    harness.backend.route("POST", TRANSFER_REJECT, lambda _call: rejected_transfer())
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    await harness.send(driver_updates.text("10"))
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT

    await harness.send(driver_updates.tap(f"staff_transfer_reject_{TRANSFER_ID}"))
    await harness.send(driver_updates.tap(f"staff_transfer_reject_yes_{TRANSFER_ID}"))

    assert len(backend_calls(harness, "POST", TRANSFER_REJECT)) == 1
    assert [key for key in user_data(harness) if key.startswith("pending_confirm_transfer")] == []
    harness.telegram.reset()
    for number in ("4", "6"):
        await harness.send(driver_updates.text(number))
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), "a count of the rejected transfer was posted"
    assert harness.telegram.shown[0].text == _curated("staff.flow_timed_out")
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None


async def test_rejecting_another_transfer_keeps_the_count_being_typed(monkeypatch):
    """Two handovers wait; the receiver counts Aziz's and rejects the other. The count is of a transfer still
    pending, so it goes on, and posts every line he typed."""
    other_reject_path = f"/api/v1/staff/bottles/transfers/{OTHER_TRANSFER_ID}/reject"
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, azizs_handover(), recorded_transfer(full=2, empty=0, id=OTHER_TRANSFER_ID))
    harness.backend.route("POST", other_reject_path, lambda _call: {**rejected_transfer(), "id": OTHER_TRANSFER_ID})
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: counted_transfer(
        declared=(10, 4), confirmed=(10, 4), status="confirmed", admins_alerted=False,
        units=[(3, "10 Litr suv", 6, 6)],
    ))
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    await harness.send(driver_updates.text("10"))

    await harness.send(driver_updates.tap(f"staff_transfer_reject_{OTHER_TRANSFER_ID}"))
    await harness.send(driver_updates.tap(f"staff_transfer_reject_yes_{OTHER_TRANSFER_ID}"))
    assert len(backend_calls(harness, "POST", other_reject_path)) == 1
    for number in ("4", "6"):
        await harness.send(driver_updates.text(number))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_CONFIRM)] == [
        {"confirmed_full": 10, "confirmed_empty": 4, "units": [{"product_id": 3, "quantity": 6}]}
    ]


@pytest.mark.parametrize(
    "error_code, message, key",
    [
        ("BOTTLE_TRANSFER_NOT_PENDING", "Transfer is not pending", "staff.error.api.transfer_already_handled"),
        ("BOTTLE_TRANSFER_NOT_RECEIVER", "Not the receiver", "staff.error.api.transfer_not_yours"),
    ],
    ids=["already-handled", "not-the-receiver"],
)
async def test_a_refused_reject_says_why_in_the_resolvers_words(monkeypatch, error_code, message, key):
    """The yes reached a transfer the backend will not reject (confirmed from another phone a moment ago, or a
    second tap on yes): its reason, with the session menu, never a fixed "failed"."""
    harness = await build_driver(monkeypatch)
    pending_inbox(harness, azizs_handover())
    harness.backend.route("POST", TRANSFER_REJECT, lambda _call: staff_backend_failure(
        message, 409, error_code=error_code,
    ))
    driver_updates, _labels = await sign_in(harness)
    await harness.send(driver_updates.tap(f"staff_transfer_reject_{TRANSFER_ID}"))
    harness.telegram.reset()

    await harness.send(driver_updates.tap(f"staff_transfer_reject_yes_{TRANSFER_ID}"))

    screen = harness.telegram.last_shown()
    assert screen.text == f"❌ {_curated(key)}"
    assert "staff_bottle_transfers_pending" in screen.callback_data()
    assert len(backend_calls(harness, "POST", TRANSFER_REJECT)) == 1


@pytest.mark.parametrize("tap", ["staff_transfer_reject_", "staff_transfer_custom_"], ids=["reject", "count"])
async def test_a_transfer_gone_from_the_inbox_is_refused_before_any_question(monkeypatch, tap):
    """The push is an hour old and the transfer was settled meanwhile: no question is asked about it, and
    nothing is posted. The inbox is the backend's word on what is still waiting."""
    harness = await build_driver(monkeypatch)
    pending_inbox(harness)
    driver_updates, _labels = await sign_in(harness)

    await harness.send(driver_updates.tap(f"{tap}{TRANSFER_ID}"))

    screen = harness.telegram.last_shown()
    assert screen.text == f"❌ {_curated('staff.error.api.transfer_already_handled')}"
    assert "staff_bottle_transfers_pending" in screen.callback_data()
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert not any(key.startswith("pending_confirm_transfer") for key in user_data(harness))
    assert not [call for call in harness.backend.calls if call.method == "POST" and "/transfers/" in call.endpoint]


async def test_other_numbers_ask_every_line_with_the_senders_number(monkeypatch):
    """One number per line, each prompt showing what the sender declared: full, empties, then the 10 L line.
    The count goes once, every line in it; a 10 L short of the sender's number is a dispute."""
    harness = await build_driver(monkeypatch)
    pending_inbox(harness, azizs_handover())
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: counted_transfer(
        declared=(10, 4), confirmed=(10, 4), status="disputed", admins_alerted=True, units=[(3, "10 Litr suv", 6, 5)],
    ))
    driver_updates, _labels = await sign_in(harness)
    await open_inbox(harness, driver_updates)

    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))
    assert harness.telegram.last_shown().text == "✏️ How many full bottles did you receive? (sent: 10)"
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    await harness.send(driver_updates.text("10"))
    assert harness.telegram.last_shown().text == "✏️ How many empty bottles did you receive? (sent: 4)"
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT

    await harness.send(driver_updates.text("4"))
    assert harness.telegram.last_shown().text == "✏️ How many 10 Litr suv did you receive? (sent: 6)"
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT

    # A keypad slip on the 10 L line is asked again, never posted.
    await harness.send(driver_updates.text("five"))
    assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP)
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)

    harness.telegram.reset()
    await harness.send(driver_updates.text("5"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_CONFIRM)] == [
        {"confirmed_full": 10, "confirmed_empty": 4, "units": [{"product_id": 3, "quantity": 5}]}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.transfer_disputed_lines").format(
        declared="18.9 L full 10 · empty 4 · 10 Litr suv 6", counted="18.9 L full 10 · empty 4 · 10 Litr suv 5",
    )
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert not any(key.startswith("pending_confirm_transfer") for key in user_data(harness))


async def test_a_menu_tap_at_the_ten_litre_count_drops_the_whole_count(monkeypatch):
    """The receiver walks away at the last prompt. Every key the count wrote goes with him, and a number he
    types later is never posted as somebody's count."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, azizs_handover())
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    for number in ("10", "4"):
        await harness.send(driver_updates.text(number))
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT

    harness.telegram.reset()
    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert _curated("staff.cash.hub_title") in harness.telegram.last_shown().text
    assert [key for key in user_data(harness) if key.startswith("pending_confirm_transfer")] == []

    await harness.send(driver_updates.text("5"))
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), (
        "a number typed after walking away confirmed somebody else's bottle transfer"
    )


async def test_one_tap_confirm_sends_as_declared(monkeypatch):
    """The receiver counted what the sender declared. One tap, and the backend is asked to take every declared
    line: the bot never re-sends a count of its own that could drift from the row. The reply names every line
    the backend credited."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, azizs_handover())
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: counted_transfer(
        declared=(10, 4), confirmed=(10, 4), status="confirmed", admins_alerted=False, units=[(3, "10 Litr suv", 6, 6)],
    ))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))
    harness.telegram.reset()

    await harness.send(driver_updates.tap(f"staff_transfer_confirm_{TRANSFER_ID}"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_CONFIRM)] == [
        {"confirm_as_declared": True}
    ]
    screen = harness.telegram.last_shown()
    assert screen.text == _curated("staff.delivery.transfer_confirmed_lines").format(
        lines="18.9 L full 10 · empty 4 · 10 Litr suv 6"
    )
    assert "staff_bottle_transfers_pending" in screen.callback_data()


async def test_an_old_two_button_confirm_still_confirms(monkeypatch):
    """A message sent before the full / empties split carries ``staff_transfer_confirm_<id>_<qty>``.
    Telegram keeps it in the chat, so it must still work: as "confirm as declared",
    whatever number its old label showed."""
    harness = await build_driver(monkeypatch)
    harness.backend.route("POST", "/api/v1/staff/bottles/transfers/7/confirm", lambda _call: counted_transfer(
        transfer_id=7, declared=(10, 0), confirmed=(10, 0), status="confirmed", admins_alerted=False,
    ))
    driver_updates, _labels = await sign_in(harness)

    await harness.send(driver_updates.tap("staff_transfer_confirm_7_10"))

    assert [call.data for call in backend_calls(harness, "POST", "/api/v1/staff/bottles/transfers/7/confirm")] == [
        {"confirm_as_declared": True}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.transfer_confirmed_lines").format(
        lines="18.9 L full 10 · empty 0"
    )


async def test_old_push_without_empty_field_renders_and_its_button_confirms(monkeypatch):
    """A push queued by the old backend names only ``declared_quantity`` and no lines. It
    renders as that many full and no empties, with the three buttons, and its Confirm confirms as declared."""
    from staff_bot.webhook_server import _render_bottle_event

    harness = await build_driver(monkeypatch)
    harness.backend.route("POST", "/api/v1/staff/bottles/transfers/7/confirm", lambda _call: counted_transfer(
        transfer_id=7, declared=(16, 0), confirmed=(16, 0), status="confirmed", admins_alerted=False,
    ))
    driver_updates, _labels = await sign_in(harness)

    text, keyboard = _render_bottle_event(
        "transfer_received", {"id": 7, "declared_quantity": 16, "sender_name": "A"}, "en"
    )

    assert text == "📥 A sent you: 18.9 L full 16 · empty 0"
    buttons = [(button.text, button.callback_data) for row in keyboard.inline_keyboard for button in row]
    assert buttons[:3] == [
        ("✅ Confirm 18.9 L full 16 · empty 0", "staff_transfer_confirm_7"),
        ("✏️ Enter other numbers", "staff_transfer_custom_7"),
        ("❌ Reject", "staff_transfer_reject_7"),
    ]

    await harness.send(driver_updates.tap(buttons[0][1]))
    assert [call.data for call in backend_calls(harness, "POST", "/api/v1/staff/bottles/transfers/7/confirm")] == [
        {"confirm_as_declared": True}
    ]


async def open_different_count(harness, driver_updates, labels):
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))
    harness.telegram.reset()
    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))


@pytest.mark.parametrize(
    "admins_alerted, key",
    [(True, "staff.delivery.transfer_disputed_lines"), (False, "staff.delivery.transfer_disputed_lines_no_admin")],
    ids=["admin-alerted", "nobody-alerted"],
)
async def test_different_count_asks_full_then_empty_and_says_an_admin_knows_only_when_one_does(
    monkeypatch, admins_alerted, key
):
    """Five full and two empties were declared; five full and one empty arrived.

    The receiver types each count, each prompt showing the sender's number. The
    dispute names every line declared and counted, as the backend recorded them.
    It says an admin was notified only when the backend says one was
    (`admins_alerted`); otherwise it asks the driver to tell one."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: counted_transfer(
        declared=(5, 2), confirmed=(5, 1), status="disputed", admins_alerted=admins_alerted,
    ))
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)

    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_received_full_qty").format(n=5)
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    # A typo here would credit the wrong number of bottles to a real session.
    harness.telegram.reset()
    await harness.send(driver_updates.text("five"))
    assert harness.telegram.last_shown().text == _curated(ZERO_OK_SLIP)
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    await harness.send(driver_updates.text("5"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_received_empty_qty").format(n=2)
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)

    harness.telegram.reset()
    await harness.send(driver_updates.text("1"))

    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_CONFIRM)] == [
        {"confirmed_full": 5, "confirmed_empty": 1}
    ]
    reply = harness.telegram.last_shown().text
    assert reply == _curated(key).format(declared="18.9 L full 5 · empty 2", counted="18.9 L full 5 · empty 1")
    if not admins_alerted:
        assert "notified" not in reply, "the reply claims an admin was told when none was"
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    for flow_key in CONFIRM_FLOW_KEYS:
        assert flow_key not in user_data(harness)


async def test_a_menu_tap_while_the_received_count_is_being_typed_never_confirms_the_transfer(monkeypatch):
    """The receiver is interrupted before answering "how many arrived?".

    Leaving this state armed is the worst of the three leaks: the next number
    the driver types anywhere becomes a confirmed bottle count on somebody
    else's handover, credited to their own session, with a dispute filed
    against the sender. Both prompts, full and empties.
    """
    for typed_before_leaving in ((), ("5",)):
        harness = await build_driver(monkeypatch)
        transfer_backend(harness)
        pending_inbox(harness, recorded_transfer(full=5, empty=2))
        driver_updates, labels = await sign_in(harness)
        await open_different_count(harness, driver_updates, labels)
        for number in typed_before_leaving:
            await harness.send(driver_updates.text(number))

        harness.telegram.reset()
        await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

        assert harness.conversation_state("staff_bottle_transfer_confirm") is None
        assert _curated("staff.cash.hub_title") in harness.telegram.last_shown().text
        assert "pending_confirm_transfer_full" not in user_data(harness)

        harness.telegram.reset()
        await harness.send(driver_updates.text("3"))
        assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), (
            "a number typed after walking away confirmed somebody else's bottle transfer"
        )


# ---------------------------------------------------------------------------
# Two handovers in one chat: the send and the receiver's count
# ---------------------------------------------------------------------------
# Both conversations live in handler group 0 and the send is registered first, so
# whenever both are armed a typed number goes to the send. Entering either one
# disarms the other (its draft is dropped), a typed number that reaches a
# conversation whose draft is gone is answered with the timed-out sentence instead
# of being swallowed or posted, and each conversation's timeout drops only its own
# draft.

CONFIRM_FLOW_KEYS = (
    "pending_confirm_transfer_id", "pending_confirm_transfer_full", "pending_confirm_transfer_empty",
    "pending_confirm_transfer_units",
)
SEND_FLOW_KEYS = ("pending_transfer_flow", "pending_transfer_receiver_id", "pending_transfer_full_qty")


def counted_a_as_four_and_two(harness):
    """Transfer A (5 full + 2 empties declared) counted as 4 + 2: disputed, an admin alerted."""
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: counted_transfer(
        declared=(5, 2), confirmed=(4, 2), status="disputed", admins_alerted=True,
    ))


def assert_a_was_counted_as_four_and_two(harness):
    assert [call.data for call in backend_calls(harness, "POST", TRANSFER_CONFIRM)] == [
        {"confirmed_full": 4, "confirmed_empty": 2}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.transfer_disputed_lines").format(
        declared="18.9 L full 5 · empty 2", counted="18.9 L full 4 · empty 2"
    )
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    for key in CONFIRM_FLOW_KEYS:
        assert key not in user_data(harness), f"{key} outlived the count it belonged to"


@pytest.mark.parametrize("typed_before_the_tap", [(), ("4",)], ids=["at-full-prompt", "at-empties-prompt"])
async def test_a_one_tap_confirm_on_another_transfer_keeps_the_count_being_typed(monkeypatch, typed_before_the_tap):
    """Two pushes in the chat. The receiver is counting transfer A ("Different
    count") when he one-taps Confirm on transfer B. B is confirmed as declared,
    and A's count is still his to finish: the two numbers he types next are
    posted for A, not dropped."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    counted_a_as_four_and_two(harness)
    harness.backend.route("POST", OTHER_TRANSFER_CONFIRM, lambda _call: counted_transfer(
        transfer_id=OTHER_TRANSFER_ID, declared=(3, 0), confirmed=(3, 0), status="confirmed", admins_alerted=False,
    ))
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    for number in typed_before_the_tap:
        await harness.send(driver_updates.text(number))

    await harness.send(driver_updates.tap(f"staff_transfer_confirm_{OTHER_TRANSFER_ID}"))

    assert [call.data for call in backend_calls(harness, "POST", OTHER_TRANSFER_CONFIRM)] == [
        {"confirm_as_declared": True}
    ]
    assert harness.telegram.last_shown().text == _curated("staff.delivery.transfer_confirmed_lines").format(
        lines="18.9 L full 3 · empty 0"
    )
    assert user_data(harness)["pending_confirm_transfer_id"] == TRANSFER_ID, (
        "confirming transfer B dropped the count the driver is typing for transfer A"
    )

    for number in ("4", "2")[len(typed_before_the_tap):]:
        await harness.send(driver_updates.text(number))

    assert_a_was_counted_as_four_and_two(harness)


@pytest.mark.parametrize("typed_before_the_tap", [(), ("5",)], ids=["at-full-prompt", "at-empties-prompt"])
async def test_different_count_tapped_mid_send_never_sends_the_receivers_numbers(monkeypatch, typed_before_the_tap):
    """Drivers A and B swap at the roadside: B is typing how many bottles he
    hands A when A's push arrives, and B taps "Enter other numbers" on it.

    The send is registered first, so while it stayed armed it took the numbers
    meant for the receiver's prompt and posted a transfer of bottles that never
    moved. Now the tap drops the send's draft. The first number lands in the
    displaced send, which says it is over instead of sending. The driver types
    the count again, and it reaches the receiver's prompt."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=12, empties=3)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    counted_a_as_four_and_two(harness)
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)
    for number in typed_before_the_tap:
        await harness.send(driver_updates.text(number))

    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_received_full_qty").format(n=5)
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT
    for key in SEND_FLOW_KEYS:
        assert key not in user_data(harness), f"the send's {key} is still armed under the receiver's prompt"

    harness.telegram.reset()
    await harness.send(driver_updates.text("4"))

    assert harness.telegram.last_shown().text == _curated("staff.flow_timed_out")
    assert harness.conversation_state("staff_bottle_transfer") is None
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    await harness.send(driver_updates.text("4"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.enter_received_empty_qty").format(n=2)
    await harness.send(driver_updates.text("2"))

    assert not backend_calls(harness, "POST", TRANSFERS), "the receiver's count was posted as a send"
    assert_a_was_counted_as_four_and_two(harness)


@pytest.mark.parametrize("typed_before_leaving", [(), ("4",)], ids=["at-full-prompt", "at-empties-prompt"])
async def test_a_send_started_mid_count_disarms_the_receivers_count(monkeypatch, typed_before_leaving):
    """The mirror: the receiver leaves his count to start a send of his own
    (Transfer, still on the session menu above). The count's draft goes. The
    send carries on, and a number that later reaches the abandoned prompt is
    answered, never posted as a count of transfer A."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    counted_a_as_four_and_two(harness)
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    for number in typed_before_leaving:
        await harness.send(driver_updates.text(number))

    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_DRIVER_SELECT
    for key in CONFIRM_FLOW_KEYS:
        assert key not in user_data(harness), f"the count's {key} is still armed under the send"

    # The picker reads no text, so a number typed now reaches the receiver's prompt behind it.
    harness.telegram.reset()
    await harness.send(driver_updates.text("3"))
    assert harness.telegram.last_shown().text == _curated("staff.flow_timed_out")
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)

    await harness.send(driver_updates.tap(f"staff_transfer_driver_{RECEIVER_DRIVER_ID}"))
    await harness.send(driver_updates.tap("staff_transfer_send_all"))
    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "send_all": True}
    ]
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)


async def test_the_send_displaced_by_a_count_times_out_in_silence_and_leaves_the_count(monkeypatch):
    """Five minutes after the Different-count tap, PTB fires the displaced send's
    timeout. It must not say "timed out" over the count the driver is typing, and
    must not drop that count's draft."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=12, empties=3)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    counted_a_as_four_and_two(harness)
    driver_updates, labels = await sign_in(harness)
    await choose_amounts(harness, driver_updates, labels)
    last_send_update = driver_updates.text("5")
    await harness.send(last_send_update)
    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))
    harness.telegram.reset()

    verdict = await fire_timeout(harness, "staff_bottle_transfer", last_send_update)

    assert verdict == ConversationHandler.END
    assert harness.telegram.shown == []
    assert user_data(harness)["pending_confirm_transfer_id"] == TRANSFER_ID
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    for number in ("4", "2"):
        await harness.send(driver_updates.text(number))
    assert not backend_calls(harness, "POST", TRANSFERS)
    assert_a_was_counted_as_four_and_two(harness)


async def test_the_count_displaced_by_a_send_times_out_in_silence_and_leaves_the_send(monkeypatch):
    """The mirror: the receiver's abandoned count times out while he is choosing
    what to send. The send keeps its colleague and its screen."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    driver_updates, labels = await sign_in(harness)
    await open_different_count(harness, driver_updates, labels)
    last_count_update = driver_updates.text("4")
    await harness.send(last_count_update)
    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))
    await harness.send(driver_updates.tap(f"staff_transfer_driver_{RECEIVER_DRIVER_ID}"))
    harness.telegram.reset()

    verdict = await fire_timeout(harness, "staff_bottle_transfer_confirm", last_count_update)

    assert verdict == ConversationHandler.END
    assert harness.telegram.shown == []
    assert user_data(harness)["pending_transfer_receiver_id"] == RECEIVER_DRIVER_ID
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_SEND_CHOICE

    await harness.send(driver_updates.tap("staff_transfer_choose"))
    await harness.send(driver_updates.text("5"))
    await harness.send(driver_updates.text("1"))
    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "full_quantity": 5, "empty_quantity": 1}
    ]
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)


@pytest.mark.parametrize("conversation", ["staff_bottle_transfer", "staff_bottle_transfer_confirm"])
async def test_a_transfer_left_alone_says_so_when_it_times_out_and_drops_its_draft(monkeypatch, conversation):
    """No sibling in the way: the timeout announces itself, hands back the main
    menu and drops the conversation's own draft."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, full=5, empties=2)
    pending_inbox(harness, recorded_transfer(full=5, empty=2))
    driver_updates, labels = await sign_in(harness)
    if conversation == "staff_bottle_transfer":
        await choose_amounts(harness, driver_updates, labels)
        own_keys = SEND_FLOW_KEYS
    else:
        await open_different_count(harness, driver_updates, labels)
        own_keys = CONFIRM_FLOW_KEYS
    last = driver_updates.text("4")
    await harness.send(last)
    harness.telegram.reset()

    await fire_timeout(harness, conversation, last)

    answer = harness.telegram.last_shown()
    assert answer.text == _curated("staff.flow_timed_out")
    assert answer.button_labels() == labels
    for key in own_keys:
        assert key not in user_data(harness), f"the expired {conversation} left {key} armed"


async def test_a_refused_transfer_confirm_says_why_instead_of_a_fixed_failure(monkeypatch):
    """The receiver taps Confirm on a transfer a second time (or on one already
    confirmed from another phone). "Confirm failed" sent them hunting for a
    fault; the refusal's own sentence tells them there is nothing left to do."""
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    pending_inbox(harness, recorded_transfer(full=4, empty=0))
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: staff_backend_failure(
        "Transfer is already confirmed", 409, error_code="BOTTLE_TRANSFER_NOT_PENDING",
    ))
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))
    harness.telegram.reset()

    await harness.send(driver_updates.tap(f"staff_transfer_confirm_{TRANSFER_ID}"))

    screen = harness.telegram.last_shown()
    assert screen.text == f"❌ {_curated('staff.error.api.transfer_already_handled')}"
    assert "staff_bottle_transfers_pending" in screen.callback_data(), (
        "the refusal screen lost the session menu it always carried"
    )


async def test_telegram_refusing_to_deliver_the_receipt_does_not_reopen_the_bottle_count_prompt(monkeypatch):
    """Telegram rejects sends routinely — flood limits, a blocked bot, a chat
    that has gone away — and it does so AFTER the session has been opened.

    Two things must survive that. The exception must not escape into PTB's
    error plumbing, and the conversation must still close. A rendering failure
    that left the quantity prompt armed would turn the driver's next message
    into a SECOND load for a truck that already has an open session — which the
    backend then refuses, stranding them at the depot with a bot that keeps
    asking for a number.
    """
    harness = await build_driver(monkeypatch)
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session(bottles_loaded=40))
    errors = capture_errors(harness)

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    harness.telegram.fail("sendMessage", "Bad Request: message is not modified")
    harness.telegram.reset()
    await harness.send(driver_updates.text("40"))

    assert [call.data for call in backend_calls(harness, "POST", SESSION_OPEN)] == [
        {"bottles_loaded": 40}
    ], "the session was not opened at all"
    attempted = [call.text for call in harness.telegram.of("sendMessage")]
    assert (
        f"{_curated('staff.delivery.bottle_session_opened_header').format(ref='SES-2026')}\n"
        f"{_curated('staff.delivery.session_bottles_line').format(n=40)}"
    ) in attempted, f"the receipt was never even composed: {attempted}"
    assert errors == [], (
        f"a Telegram send failure surfaced as a handler error after the write "
        f"had already succeeded: {errors}"
    )
    assert harness.conversation_state("staff_bottle_loaded") is None, (
        "the quantity prompt survived a rendering failure and will parse the "
        "driver's next message as another load"
    )

    # And the write is not repeated when the driver, seeing nothing, types again.
    await harness.send(driver_updates.text("40"))
    assert len(backend_calls(harness, "POST", SESSION_OPEN)) == 1, (
        "a driver who got no receipt was allowed to open a second session by "
        "retyping the count"
    )


# ===========================================================================
# Sharing a session with a colleague (co-driver membership)
# ===========================================================================


async def test_a_driver_who_joined_a_colleagues_session_can_find_the_way_out(driver):
    """A driver who joins a colleague's session must be able to leave it.

    Two drivers sharing a truck share ONE bottle inventory, and everything the
    joined driver delivers or collects is counted against the owner's session.
    When they split up — different vans in the afternoon, end of a covering
    shift — staying joined means their work keeps landing on someone else's
    ledger.

    ``bottles_leave_session`` and ``bottles_membership_status`` were both
    registered in ``staff_bot/bot.py`` and both emitted by NOTHING: the only
    keyboard carrying ``bottles_leave_session`` was drawn by
    ``show_membership_status``, which was itself only reachable through
    ``bottles_membership_status``, which no keyboard in the repo produced. A
    handler nothing can call is the same as no handler, so the way out did not
    exist and drivers had to ask an admin.

    The entry now hangs off "📊 My bottle accountability" in the Cash hub —
    the one screen in the bot that answers "what am I holding, and under whose
    session?", which is exactly the question a driver asks just before wanting
    out. This drives the whole path: hub → accountability → membership → leave.
    """
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver.backend.route("GET", SESSION_MEMBERSHIP, lambda _call: dict(MEMBERSHIP_ROW))
    driver.backend.route("POST", SESSION_LEAVE, lambda _call: {"status": "left"})

    driver_updates, labels = await sign_in(driver)
    hub = await open_cash_hub(driver, driver_updates, labels)
    assert "staff_bottle_my_accountability" in hub

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    session_screen = driver.telegram.last_shown()
    assert "bottles_membership_status" in session_screen.callback_data(), (
        "the accountability screen offers no way to see whose session the driver is "
        f"working under: {session_screen.callback_data()}"
    )

    await driver.send(driver_updates.tap("bottles_membership_status"))
    membership = driver.telegram.last_shown()
    assert "Bek Toshev" in membership.text, (
        "the membership screen does not name the colleague whose inventory this is"
    )
    assert "bottles_leave_session" in membership.callback_data(), (
        f"there is still no way out of a colleague's session: {membership.callback_data()}"
    )

    await driver.send(driver_updates.tap("bottles_leave_session"))
    assert backend_calls(driver, "POST", SESSION_LEAVE), (
        "tapping Leave Session posted nothing; the driver is still on the colleague's ledger"
    )
    assert _curated("staff.bottles.left_session") in driver.telegram.last_shown().text


async def test_the_membership_screen_shows_the_owners_truck_in_backend_numbers(driver):
    """Spec 2026-11-09 §4.4 (U1). The screen read "Using X's session, N bottles available"
    with N = `current_inventory`, which counts empties since the split: a truck with
    11 full and 30 empties told the co-driver 41 bottles were available to deliver."""
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver.backend.route("GET", SESSION_MEMBERSHIP, lambda _call: dict(MEMBERSHIP_ROW))
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))

    await driver.send(driver_updates.tap("bottles_membership_status"))

    assert driver.telegram.last_shown().text.splitlines() == [
        f"🤝 <b>{_curated('staff.bottles.current_membership_title')}</b>",
        "",
        owner_line("Bek Toshev"),
        *truck_lines(full=11, empties=30, free_full=5),
    ]
    # The Leave copy carries its own door: the button shows it once, not "🚪 🚪 Leave Session".
    assert driver.telegram.last_shown().button_labels() == [
        _curated("staff.bottles.leave_session"),
        _curated("staff.back"),
    ]


@pytest.mark.parametrize(
    "details, sentence",
    [
        ({"count": 2}, lambda: _curated("staff.error.api.leave_has_promised_stops_detail").format(count=2)),
        (None, lambda: _curated("staff.error.api.leave_has_promised_stops")),
    ],
    ids=["with-the-count", "without-details"],
)
async def test_leaving_a_colleagues_session_with_orders_on_the_way_is_refused_in_the_drivers_words(
    driver, details, sentence
):
    """Spec A8: the bottles of a co-driver's On-the-way and Arrived stops are promised
    from the owner's truck, so he may not leave the session until they are delivered
    (BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS, with the number of such stops). The
    refusal is a translated sentence that says what to do, never the backend's English."""
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver.backend.route("GET", SESSION_MEMBERSHIP, lambda _call: dict(MEMBERSHIP_ROW))
    driver.backend.route("POST", SESSION_LEAVE, lambda _call: staff_backend_failure(
        "Finish your 2 stop(s) on the way before leaving the session", 409,
        error_code="BOTTLE_SESSION_LEAVE_HAS_PROMISED_STOPS", details=details,
    ))
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    await driver.send(driver_updates.tap("bottles_membership_status"))
    driver.telegram.reset()

    await driver.send(driver_updates.tap("bottles_leave_session"))

    assert len(backend_calls(driver, "POST", SESSION_LEAVE)) == 1
    screen = driver.telegram.last_shown()
    assert screen.text == f"❌ {sentence()}"
    assert screen.callback_data() == ["staff_back_to_main"]


async def test_a_session_owner_can_reach_the_invite_screen_from_their_own_session(driver):
    """The mirror of the test above, for the driver who OWNS the session.

    ``bottles_invite_driver`` had the same shape of break: registered in
    ``staff_bot/bot.py``, emitted only by the Cancel button of the confirmation
    screen INSIDE the invite flow — i.e. reachable only once you were already
    somewhere you could not get to.

    Same placement argument: the owner's accountability screen is where they
    look at their own session, and "let my colleague deliver against this" is a
    decision about that session.
    """
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver.backend.route(
        "GET", AVAILABLE_DRIVERS,
        lambda _call: [{"user_id": RECEIVER_DRIVER_ID, "name": "Bekzod Rahimov"}],
    )

    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    session_screen = driver.telegram.last_shown()
    assert "bottles_invite_driver" in session_screen.callback_data(), (
        f"a session owner cannot reach the invite screen: {session_screen.callback_data()}"
    )

    await driver.send(driver_updates.tap("bottles_invite_driver"))
    picker = driver.telegram.last_shown()
    assert f"bottles_invite_confirm_{RECEIVER_DRIVER_ID}" in picker.callback_data(), (
        f"the invite screen lists no drivers to invite: {picker.callback_data()}"
    )


async def test_the_accountability_screen_still_offers_the_session_actions_it_always_did(driver):
    """The co-driver rows are ADDED to the session menu, not a second copy of it.

    ``DeliveryKeyboards.bottle_session_menu`` is the one definition of "what can
    I do with my session", rendered from a dozen places in
    ``bottle_collection.py``. The accountability screen extends it rather than
    rebuilding it, so a button added there keeps showing up here.
    """
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    offered = driver.telegram.last_shown().callback_data()

    for expected in (
        "staff_bottle_session_load",
        "staff_bottle_session_return",
        "staff_bottle_transfer_start",
        "staff_bottle_transfers_pending",
        "staff_cash_hub",
    ):
        assert expected in offered, (
            f"{expected} disappeared from the accountability screen: {offered}"
        )


@pytest.mark.parametrize(
    "error_code, message, expected_key",
    [
        ("BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND", "No active co-driver session membership",
         "staff.bottles.no_active_membership"),
        ("BOTTLE_SESSION_MEMBERSHIP_CLOSED", "The session you joined is no longer open",
         "staff.error.api.joined_session_closed"),
    ],
)
async def test_only_not_being_a_member_reads_as_no_membership(driver, error_code, message, expected_key):
    """Both answers are 404s. "You are not in a session" is the empty state; "the
    owner closed the session you joined" is news the driver needs, and used to
    be flattened into the empty state by the status code alone."""
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver.backend.route(
        "GET", SESSION_MEMBERSHIP,
        lambda _call: staff_backend_failure(message, 404, error_code=error_code),
    )
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    driver.telegram.reset()

    await driver.send(driver_updates.tap("bottles_membership_status"))

    assert driver.telegram.last_shown().text == _curated(expected_key)


async def test_an_invite_refused_for_want_of_a_session_offers_the_way_to_open_one(driver):
    """A driver with no open session taps "Invite co-driver" from the
    accountability screen. The refusal names the fix, and the screen carries the
    button to it (the code's remedy) above the Back it always had — a sentence
    telling them to open a session with only Back under it is a dead end."""
    driver.backend.route(
        "GET", AVAILABLE_DRIVERS,
        lambda _call: staff_backend_failure(
            "You must have an open bottle session to invite co-drivers", 409,
            error_code="BOTTLE_SESSION_REQUIRED_TO_INVITE",
        ),
    )
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    driver.telegram.reset()

    await driver.send(driver_updates.tap("bottles_invite_driver"))

    screen = driver.telegram.last_shown()
    assert screen.text == f"❌ {_curated('staff.error.api.open_session_to_invite')}"
    assert screen.callback_data() == ["staff_bottle_my_accountability", "staff_back_to_main"], (
        f"the refusal does not offer the way to open a session: {screen.callback_data()}"
    )


async def test_the_transfer_picker_lists_every_recipient_with_their_session_state(monkeypatch):
    """Twelve active drivers: all twelve are offered (the old picker cut at ten),
    each with the hint the backend published. A driver who already has a
    session is a recipient now. The old list hid exactly those drivers."""
    harness = await build_driver(monkeypatch)
    statuses = ["own_session", "co_driver", "none"]
    rows = [
        {"user_id": 100 + i, "name": f"Driver {i}", "bottle_status": statuses[i % 3]}
        for i in range(12)
    ]
    transfer_backend(harness, drivers=rows)
    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))

    picker = harness.telegram.last_shown()
    assert [f"staff_transfer_driver_{100 + i}" for i in range(12)] == picker.callback_data()[:12]
    labels_shown = picker.button_labels()
    assert labels_shown[0] == "🚚 Driver 0"
    assert labels_shown[1] == "🤝 Driver 1"
    assert labels_shown[2] == "👤 Driver 2"
    assert backend_calls(harness, "GET", AVAILABLE_DRIVERS) == []


async def test_a_recipient_deactivated_after_the_picker_was_drawn_is_refused_and_the_flow_ends(monkeypatch):
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    harness.backend.route("POST", TRANSFERS, lambda _call: staff_backend_failure(
        "This driver can't receive bottles right now", 400, error_code="BOTTLE_TRANSFER_RECEIVER_INVALID",
    ))
    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)

    await harness.send(driver_updates.tap("staff_transfer_send_all"))

    refusal = harness.telegram.last_shown()
    assert _curated("staff.error.api.transfer_receiver_invalid") in refusal.text
    assert "staff_bottle_my_accountability" in refusal.callback_data()
    assert harness.conversation_state("staff_bottle_transfer") is None


# ===========================================================================
# Asking to join a colleague's session
# ===========================================================================


async def test_a_driver_with_no_session_finds_join_on_the_accountability_screen_and_sends_a_request(driver):
    driver.backend.route("GET", JOINABLE, lambda _call: [dict(JOINABLE_ROW)])
    driver.backend.route("POST", JOIN_REQUEST, lambda _call: dict(JOINABLE_ROW))
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))
    assert "bottles_join_session" in driver.telegram.last_shown().callback_data()

    await driver.send(driver_updates.tap("bottles_join_session"))
    assert "bottles_join_confirm_12" in driver.telegram.last_shown().callback_data()

    await driver.send(driver_updates.tap("bottles_join_confirm_12"))
    confirm = driver.telegram.last_shown()
    assert _curated("staff.bottles.join_session_confirm_title") in confirm.text
    # The owner's truck in the backend's numbers: what joining would let this driver deliver from.
    for expected in truck_lines(full=7, empties=2, free_full=3):
        assert expected in confirm.text.splitlines(), f"{expected!r} missing from {confirm.text!r}"
    assert "Ali &lt;&amp;&gt; Co" in confirm.text and "Ali <&> Co" not in confirm.text
    assert "bottles_join_request_12" in confirm.callback_data()
    assert not any(c.startswith("bottles_join_execute_") for c in confirm.callback_data())

    await driver.send(driver_updates.tap("bottles_join_request_12"))

    assert [c.data for c in backend_calls(driver, "POST", JOIN_REQUEST)] == [{"session_id": 12}]
    sent = driver.telegram.last_shown().text
    assert "Ali &lt;&amp;&gt; Co" in sent and "Ali <&> Co" not in sent
    assert _curated("staff.bottles.join_request_sent").split("{name}")[0].strip() in sent


async def test_the_joinable_list_shows_each_truck_in_backend_numbers(driver):
    """The first join screen, spec 2026-11-09 §4.4 (U1). Each button read
    `current_inventory/bottles_loaded`, which counts empties since the split: a truck
    with 11 full and 30 empties read "41/41", a full load, to a driver choosing whose
    truck to deliver from. Every number now is a field the backend published."""
    driver.backend.route("GET", JOINABLE, lambda _call: [dict(JOINABLE_ROW), dict(JOINABLE_ROW_2)])
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))

    await driver.send(driver_updates.tap("bottles_join_session"))

    screen = driver.telegram.last_shown()
    assert screen.text.splitlines() == [
        f"<b>{_curated('staff.bottles.choose_session_to_join')}</b>",
        "",
        owner_line("Ali &lt;&amp;&gt; Co"),
        *truck_lines(full=7, empties=2, free_full=3),
        "",
        owner_line("Bek Toshev"),
        *truck_lines(full=11, empties=30, free_full=5),
    ]
    assert screen.button_labels() == ["📦 Ali <&> Co", "📦 Bek Toshev", _curated("staff.back")]
    assert screen.callback_data() == ["bottles_join_confirm_12", "bottles_join_confirm_14", "staff_back_to_main"]


async def test_an_owner_does_not_see_join_on_their_own_session(driver):
    driver.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)

    await driver.send(driver_updates.tap("staff_bottle_my_accountability"))

    assert "bottles_join_session" not in driver.telegram.last_shown().callback_data()


async def test_a_session_that_closed_before_the_request_was_sent_says_so_and_offers_the_list(driver):
    driver.backend.route("POST", JOIN_REQUEST, lambda _call: staff_backend_failure(
        "Can only join an OPEN session", 400, error_code="BOTTLE_SESSION_NOT_OPEN",
    ))
    driver_updates, _labels = await sign_in(driver)

    await driver.send(driver_updates.tap("bottles_join_request_12"))

    screen = driver.telegram.last_shown()
    assert _curated("staff.error.api.bottle_session_closed") in screen.text
    assert screen.callback_data() == ["bottles_join_session"]


@pytest.mark.parametrize("verdict, endpoint, reply, key", [
    ("ok", JOIN_APPROVE, {"member_name": "Vali <b>"}, "staff.bottles.join_request_approved_owner"),
    ("no", JOIN_DECLINE, {"requester_id": 77, "requester_name": "Vali <b>"}, "staff.bottles.join_request_declined_owner"),
])
async def test_the_owner_answers_from_the_pushed_message_and_the_buttons_go_away(driver, verdict, endpoint, reply, key):
    driver.backend.route("POST", endpoint, lambda _call: dict(reply))
    driver_updates, _labels = await sign_in(driver)

    await driver.send(driver_updates.tap(f"bottles_jr_{verdict}_12_77"))

    assert [c.data for c in backend_calls(driver, "POST", endpoint)] == [{"session_id": 12, "requester_id": 77}]
    answered = driver.telegram.last_shown()
    assert "Vali &lt;b&gt;" in answered.text
    assert _curated(key).split("{name}")[0].strip() in answered.text
    assert not any(c.startswith("bottles_jr_") for c in answered.callback_data())


def _tap_on_pushed_request(driver_updates, verdict):
    """The owner's tap on the pushed request, whose message still carries both buttons."""
    return driver_updates._build({
        "update_id": driver_updates._next_update_id(),
        "callback_query": {
            "id": f"cb{driver_updates._update_id}",
            "from": driver_updates._user,
            "chat_instance": "test-chat-instance",
            "data": f"bottles_jr_{verdict}_12_77",
            "message": {
                "message_id": driver_updates._message_id,
                "date": 1_700_000_000,
                "chat": {"id": driver_updates.chat_id, "type": "private"},
                "from": {"id": 42, "is_bot": True, "first_name": "BlueStream"},
                "text": "Ali asks to join your session",
                "reply_markup": {"inline_keyboard": [[
                    {"text": "Approve", "callback_data": "bottles_jr_ok_12_77"},
                    {"text": "Decline", "callback_data": "bottles_jr_no_12_77"},
                ]]},
            },
        },
    })


async def test_a_transient_failure_keeps_the_owners_approve_and_decline_buttons(driver):
    driver.backend.route("POST", JOIN_APPROVE, lambda _call: staff_backend_failure("boom", 503))
    driver_updates, _labels = await sign_in(driver)

    await driver.send(_tap_on_pushed_request(driver_updates, "ok"))

    shown = driver.telegram.last_shown()
    assert "bottles_jr_ok_12_77" in shown.callback_data()
    assert "bottles_jr_no_12_77" in shown.callback_data()


async def test_a_coded_refusal_still_replaces_the_buttons_with_back(driver):
    driver.backend.route("POST", JOIN_APPROVE, lambda _call: staff_backend_failure(
        "This join request was for a session you have since closed", 409, error_code="BOTTLE_JOIN_REQUEST_STALE",
    ))
    driver_updates, _labels = await sign_in(driver)

    await driver.send(_tap_on_pushed_request(driver_updates, "ok"))

    assert not any(c.startswith("bottles_jr_") for c in driver.telegram.last_shown().callback_data())


async def test_a_stale_approve_says_so(driver):
    driver.backend.route("POST", JOIN_APPROVE, lambda _call: staff_backend_failure(
        "This join request was for a session you have since closed", 409, error_code="BOTTLE_JOIN_REQUEST_STALE",
    ))
    driver_updates, _labels = await sign_in(driver)

    await driver.send(driver_updates.tap("bottles_jr_ok_12_77"))

    assert _curated("staff.error.api.join_request_stale") in driver.telegram.last_shown().text


async def test_an_old_instant_join_button_in_the_chat_history_opens_the_request_list(driver):
    driver.backend.route("GET", JOINABLE, lambda _call: [dict(JOINABLE_ROW)])
    driver_updates, _labels = await sign_in(driver)
    update = driver_updates.tap("bottles_join_execute_12")
    assert driver.handlers_matching(update), "an old Join button would spin with no answer"

    await driver.send(update)

    assert "bottles_join_confirm_12" in driver.telegram.last_shown().callback_data()
    assert backend_calls(driver, "POST", "/api/v1/staff/bottles/session/join") == []
