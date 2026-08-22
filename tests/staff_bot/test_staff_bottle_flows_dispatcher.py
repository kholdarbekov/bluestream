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

from staff_bot.handlers.delivery.bottle_collection import (
    BOTTLE_COLLECTION_SEARCH_INPUT,
    BOTTLE_SESSION_LOADED_QTY_INPUT,
    BOTTLE_SESSION_RETURNED_QTY_INPUT,
    BOTTLE_TRANSFER_CONFIRM_QTY_INPUT,
    BOTTLE_TRANSFER_DRIVER_SELECT,
    BOTTLE_TRANSFER_QTY_INPUT,
)
from staff_bot.utils.formatters import format_quantity
from staff_bot.utils.search import detect_search_type

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

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
    "staff.delivery.bottle_session_opened",
    "staff.delivery.bottle_session_closed",
    "staff.delivery.bottle_session_already_open",
    "staff.delivery.no_active_bottle_session",
    "staff.delivery.discrepancy_zero",
    "staff.delivery.discrepancy_nonzero",
    # The session summary block rendered above the return prompt.
    "staff.delivery.session_ref_label",
    "staff.delivery.session_started_label",
    "staff.delivery.bottles_loaded_label",
    "staff.delivery.bottles_delivered_label",
    "staff.delivery.bottles_collected_label",
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
    "staff.delivery.select_transfer_driver",
    "staff.delivery.enter_transfer_qty",
    "staff.delivery.transfer_qty_exceeds_available",
    "staff.delivery.bottle_transfer_initiated",
    "staff.delivery.no_bottles_to_transfer",
    "staff.delivery.no_active_drivers",
    "staff.delivery.pending_transfers_title",
    "staff.delivery.pending_transfer_line",
    "staff.delivery.transfer_confirm_button",
    "staff.delivery.transfer_custom_count_button",
    "staff.delivery.enter_actual_received_qty",
    "staff.delivery.transfer_confirmed",
    "staff.delivery.transfer_disputed",
    "staff.operator.search_too_short",
    "staff.error.api.service_unavailable",
    "staff.error.api.validation",
    "staff.error_occurred",
    "staff.session_expired",
    "staff.cancelled",
    # Co-driver session membership: two trucks, one inventory.
    "staff.bottles.current_membership_title",
    "staff.bottles.current_membership",
    "staff.bottles.leave_session",
    "staff.bottles.left_session",
    "staff.bottles.no_active_membership",
    "staff.bottles.invite_codriver",
    "staff.bottles.choose_driver_to_invite",
    "staff.bottles.no_drivers_to_invite",
    "staff.common.unknown_driver",
    "staff.common.driver_number",
)


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
SESSION_CLOSE = "/api/v1/staff/bottles/session/close"
AVAILABLE_DRIVERS = "/api/v1/staff/bottles/sessions/available-drivers"
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


def open_session(**overrides) -> dict:
    """A driver mid-shift: 40 loaded, 9 still on the truck."""
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
        "current_inventory": 9,
    }
    session.update(overrides)
    return session


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


async def open_cash_hub(harness, driver_updates, labels):
    """Tap 💰 Cash on the reply keyboard and return the hub's callback data."""
    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))
    hub = harness.telegram.last_shown()
    assert _curated("staff.cash.hub_title") in hub.text
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

    assert driver.telegram.last_shown().text == _curated(
        "staff.delivery.bottle_session_opened"
    ).format(count=40, ref="SES-2026")
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
        assert driver.telegram.last_shown().text == _curated(
            "staff.delivery.invalid_bottle_count"
        ), f"{typo!r} was not refused with the invalid-count copy"
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


async def test_a_zero_or_negative_load_never_leaves_the_bot(driver):
    """"-5" and "0" are not counts, they are typos or a driver testing the bot.

    A zero-bottle session is a session that exists but can never be
    reconciled, and a negative one would make the ledger read as if the
    warehouse owed the driver bottles. The backend refuses both (`gt=0` on
    ``DriverBottleSessionOpenRequest``) — but relying on that means a round
    trip and a generic server error at the depot instead of an instant,
    localized "enter a valid positive number".
    """
    driver_updates, labels = await sign_in(driver)
    await open_cash_hub(driver, driver_updates, labels)
    await driver.send(driver_updates.tap("staff_bottle_log_loaded"))

    for refused in ("0", "-5", "-1"):
        driver.telegram.reset()
        await driver.send(driver_updates.text(refused))
        assert driver.telegram.last_shown().text == _curated("staff.delivery.invalid_bottle_count")
        assert driver.conversation_state("staff_bottle_loaded") == BOTTLE_SESSION_LOADED_QTY_INPUT

    assert not backend_calls(driver, "POST", SESSION_OPEN), (
        "a non-positive bottle count was posted to the ledger"
    )


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
    answered with ``staff.delivery.invalid_bottle_count``, exactly as the lower
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
    assert driver.telegram.last_shown().text == _curated(
        "staff.delivery.invalid_bottle_count"
    ), "the driver was not told what was wrong with what they typed"
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
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session())
    harness.backend.route("POST", SESSION_OPEN, lambda _call: open_session())

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))

    assert harness.telegram.last_shown().text == _curated(
        "staff.delivery.bottle_session_already_open"
    ).format(started="2026-08-21 06:30", loaded=40)
    assert harness.conversation_state("staff_bottle_loaded") is None, (
        "the refusal left the quantity prompt armed"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.text("40"))
    assert not backend_calls(harness, "POST", SESSION_OPEN), (
        "a number typed after the refusal opened a second session for the same truck"
    )


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
    assert harness.telegram.last_shown().text == _curated("staff.delivery.invalid_bottle_count")
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
    assert harness.telegram.last_shown().text == _curated(
        "staff.delivery.invalid_bottle_count"
    ), "the driver was not told what was wrong with what they typed"
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


# ===========================================================================
# Transferring bottles between drivers
# ===========================================================================


def transfer_backend(harness, *, inventory=9, drivers=None):
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(current_inventory=inventory))
    harness.backend.route(
        "GET",
        AVAILABLE_DRIVERS,
        lambda _call: drivers if drivers is not None else [
            {"user_id": RECEIVER_DRIVER_ID, "name": "Bekzod Rahimov"},
        ],
    )
    harness.backend.route("POST", TRANSFERS, lambda _call: {
        "id": TRANSFER_ID,
        "transfer_ref": "TRF-31-2026",
        "status": "pending",
        "declared_quantity": 4,
    })


async def start_transfer(harness, driver_updates, labels):
    """Cash hub → load screen → session menu → transfer, ending on the qty prompt."""
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

    await harness.send(driver_updates.tap(f"staff_transfer_driver_{RECEIVER_DRIVER_ID}"))
    harness.telegram.reset()
    return picker


async def test_a_driver_hands_four_bottles_to_a_colleague_and_both_the_receiver_and_the_count_reach_the_backend(monkeypatch):
    """Two trucks meet on the road and bottles change hands.

    Until the receiver confirms, this write is the ONLY record that the bottles
    left the sender's accountability. Both fields matter equally: a wrong
    ``receiver_driver_id`` credits a driver who never took the bottles and
    leaves the real one short at reconciliation.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    driver_updates, labels = await sign_in(harness)

    await start_transfer(harness, driver_updates, labels)
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_QTY_INPUT

    await harness.send(driver_updates.text("4"))

    posts = backend_calls(harness, "POST", TRANSFERS)
    assert len(posts) == 1
    assert posts[0].data == {"receiver_driver_id": RECEIVER_DRIVER_ID, "quantity": 4}

    assert harness.telegram.last_shown().text == _curated(
        "staff.delivery.bottle_transfer_initiated"
    ).format(qty=4, ref="TRF-31-2")
    assert harness.conversation_state("staff_bottle_transfer") is None

    # Walking away and typing the same number again must not hand over a second
    # batch — the conversation is over and nothing is left listening.
    harness.telegram.reset()
    await harness.send(driver_updates.text("4"))
    assert len(backend_calls(harness, "POST", TRANSFERS)) == 1


async def test_a_transfer_bigger_than_the_truck_holds_is_refused_locally_and_the_prompt_survives(monkeypatch):
    """You cannot hand over bottles you do not have.

    The check is done in the bot because the driver is standing next to the
    other truck: a round trip and a generic backend conflict is a worse answer
    than an immediate "you only have 9". Refusing must not end the flow — the
    driver's real intent was almost always a smaller number.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, inventory=9)
    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)

    for refused, expected in (
        ("50", _curated("staff.delivery.transfer_qty_exceeds_available").format(available=9)),
        ("10", _curated("staff.delivery.transfer_qty_exceeds_available").format(available=9)),
        ("four", _curated("staff.delivery.invalid_bottle_count")),
        ("0", _curated("staff.delivery.invalid_bottle_count")),
        ("-4", _curated("staff.delivery.invalid_bottle_count")),
    ):
        harness.telegram.reset()
        await harness.send(driver_updates.text(refused))
        assert harness.telegram.last_shown().text == expected, f"{refused!r} got the wrong refusal"
        assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_QTY_INPUT, (
            f"{refused!r} knocked the driver out of the transfer"
        )
        assert not backend_calls(harness, "POST", TRANSFERS)

    harness.telegram.reset()
    await harness.send(driver_updates.text("9"))
    assert [call.data for call in backend_calls(harness, "POST", TRANSFERS)] == [
        {"receiver_driver_id": RECEIVER_DRIVER_ID, "quantity": 9}
    ]


async def test_a_driver_with_an_empty_truck_is_told_there_is_nothing_to_transfer_and_no_prompt_is_left_open(monkeypatch):
    """Nothing on board. The flow must refuse and close.

    A refusal that leaves the quantity state armed is worse than useless: the
    driver reads "no bottles to transfer", types something unrelated, and the
    bot answers with a bottle-count error about a transfer they abandoned.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, inventory=0)
    driver_updates, labels = await sign_in(harness)

    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))

    assert harness.telegram.last_shown().text == _curated("staff.delivery.no_bottles_to_transfer")
    assert harness.conversation_state("staff_bottle_transfer") is None
    assert not backend_calls(harness, "GET", AVAILABLE_DRIVERS), (
        "the driver list was fetched for a transfer that can never happen"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.text("4"))
    assert not backend_calls(harness, "POST", TRANSFERS)


async def test_a_menu_tap_while_the_transfer_quantity_is_being_typed_cancels_the_handover(monkeypatch):
    """The other driver drives off mid-conversation and the sender gives up.

    Same state-leak class as the load flow, with worse consequences: a stale
    transfer prompt turns the next number the driver types anywhere in the bot
    into a handover of that many bottles to a colleague they picked minutes
    ago.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_QTY_INPUT

    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_bottle_transfer") is None, (
        "the transfer quantity prompt is still armed after the driver navigated away"
    )
    assert _curated("staff.cash.hub_title") in harness.telegram.last_shown().text

    harness.telegram.reset()
    await harness.send(driver_updates.text("4"))
    assert not backend_calls(harness, "POST", TRANSFERS), (
        "a number typed after leaving the flow handed four bottles to another driver"
    )


async def test_walking_away_from_the_driver_picker_disarms_the_stale_truck_count(monkeypatch):
    """Was: ``test_RATCHET_walking_away_from_the_driver_picker_leaves_the_transfer_armed_with_a_stale_truck_count``.

    ``BOTTLE_TRANSFER_DRIVER_SELECT`` is the one bottle state with no
    ``menu_escape`` and no text handler, so a main-menu tap there is handled by
    the global router — which cannot end a conversation it is not part of, and
    the picker message keeps live buttons in the scrollback. That half is a
    wiring fix in ``staff_bot/bot.py`` and is still pinned by
    ``tests/staff_bot/test_staff_flow_state_and_escapes.py``.

    The half that made it dangerous was ``pending_transfer_available``: stamped
    from the open session when the flow opened, used by
    ``receive_transfer_quantity`` as the "you only have N" ceiling, and cleared
    by nothing. So a tap on the stale picker hours later re-entered the quantity
    prompt quoting the MORNING's inventory and handed over nine bottles that had
    long since left the truck.

    The key is registered with the flow-state SSOT now, so walking away takes
    the ceiling with it and the stale picker cannot spend bottles.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness, inventory=9)
    driver_updates, labels = await sign_in(harness)

    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfer_start"))
    assert harness.conversation_state("staff_bottle_transfer") == BOTTLE_TRANSFER_DRIVER_SELECT
    assert user_data(harness)["pending_transfer_available"] == 9, (
        "fixture: opening the picker stamps the ceiling this test is about"
    )

    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))
    assert "pending_transfer_available" not in user_data(harness), (
        "the morning's truck load survived the driver walking away"
    )

    # The afternoon happens: the truck is empty now.
    harness.backend.route("GET", SESSION_CURRENT, lambda _call: open_session(current_inventory=0))

    harness.telegram.reset()
    await harness.send(driver_updates.tap(f"staff_transfer_driver_{RECEIVER_DRIVER_ID}"))
    stale_tap = harness.telegram.last_shown()
    assert stale_tap.text == _curated("staff.cancelled"), (
        f"a tap on the abandoned picker must say the transfer is over, not open a "
        f"quantity prompt: {stale_tap.text!r}"
    )
    assert stale_tap.callback_data(), (
        "the driver was left on a dead-end message with no way back to the session menu"
    )
    assert harness.conversation_state("staff_bottle_transfer") is None, (
        "the stale tap re-armed the abandoned transfer"
    )

    await harness.send(driver_updates.text("9"))
    assert not backend_calls(harness, "POST", TRANSFERS), (
        "nine bottles that are no longer on the truck were handed over"
    )


async def test_a_transfer_the_other_driver_never_confirms_credits_nobody_and_is_still_waiting_in_their_inbox(monkeypatch):
    """The handover the receiver forgets about — the normal failure, not a rare one.

    A transfer is two writes: the sender declares it, the receiver confirms it.
    Between them the bottles belong to nobody, and the sender is still
    accountable. Nothing in the bot may quietly close that gap: no
    auto-confirm, no second POST, and the transfer must still be sitting in the
    receiver's inbox with both answers available — accept the declared count,
    or say what actually arrived.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    harness.backend.route("GET", TRANSFERS_PENDING, lambda _call: [
        {
            "id": TRANSFER_ID,
            "transfer_ref": "TRF-31-2026",
            "declared_quantity": 4,
            "sender_name": "Aziz Karimov",
            "status": "pending",
        },
    ])

    driver_updates, labels = await sign_in(harness)
    await start_transfer(harness, driver_updates, labels)
    await harness.send(driver_updates.text("4"))

    initiated = harness.telegram.last_shown()
    assert "staff_bottle_transfers_pending" in initiated.callback_data(), (
        "after declaring a transfer the driver has no route to the pending inbox"
    )

    harness.telegram.reset()
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))

    inbox = harness.telegram.last_shown()
    assert _curated("staff.delivery.pending_transfers_title") in inbox.text
    assert _curated("staff.delivery.pending_transfer_line").format(
        sender="Aziz Karimov", qty=4, ref="TRF-31-2"
    ) in inbox.text
    assert f"staff_transfer_confirm_{TRANSFER_ID}_4" in inbox.callback_data()
    assert f"staff_transfer_custom_{TRANSFER_ID}" in inbox.callback_data(), (
        "the receiver can only rubber-stamp the declared count; there is no way to "
        "report that fewer bottles actually arrived"
    )

    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), (
        "an unconfirmed transfer was confirmed by merely looking at it"
    )
    assert len(backend_calls(harness, "POST", TRANSFERS)) == 1


async def test_the_receiver_reports_a_smaller_count_and_the_dispute_names_both_numbers(monkeypatch):
    """Five bottles were declared, three arrived. This is how the argument starts.

    The receiver's typed count is what gets credited to their session, and the
    difference is escalated to an admin. The screen must therefore show BOTH
    numbers: a message that only says "3 confirmed" leaves the receiver unable
    to tell whether the sender's claim of 5 was recorded at all.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    harness.backend.route("GET", TRANSFERS_PENDING, lambda _call: [
        {
            "id": TRANSFER_ID,
            "transfer_ref": "TRF-31-2026",
            "declared_quantity": 5,
            "sender_name": "Bekzod Rahimov",
            "status": "pending",
        },
    ])
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: {
        "id": TRANSFER_ID,
        "status": "disputed",
        "declared_quantity": 5,
        "confirmed_quantity": 3,
    })

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))

    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))
    assert harness.telegram.last_shown().text == _curated(
        "staff.delivery.enter_actual_received_qty"
    )
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_QTY_INPUT

    # A typo here would credit the wrong number of bottles to a real session.
    harness.telegram.reset()
    await harness.send(driver_updates.text("three"))
    assert harness.telegram.last_shown().text == _curated("staff.delivery.invalid_bottle_count")
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_QTY_INPUT
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM)

    harness.telegram.reset()
    await harness.send(driver_updates.text("3"))

    posts = backend_calls(harness, "POST", TRANSFER_CONFIRM)
    assert len(posts) == 1
    assert posts[0].data == {"confirmed_quantity": 3}
    assert harness.telegram.last_shown().text == _curated(
        "staff.delivery.transfer_disputed"
    ).format(declared=5, qty=3)
    assert harness.conversation_state("staff_bottle_transfer_confirm") is None


async def test_a_menu_tap_while_the_received_count_is_being_typed_never_confirms_the_transfer(monkeypatch):
    """The receiver is interrupted before answering "how many arrived?".

    Leaving this state armed is the worst of the three leaks: the next number
    the driver types anywhere becomes a confirmed bottle count on somebody
    else's handover, credited to their own session, with a dispute filed
    against the sender.
    """
    harness = await build_driver(monkeypatch)
    transfer_backend(harness)
    harness.backend.route("GET", TRANSFERS_PENDING, lambda _call: [
        {
            "id": TRANSFER_ID,
            "transfer_ref": "TRF-31-2026",
            "declared_quantity": 5,
            "sender_name": "Bekzod Rahimov",
            "status": "pending",
        },
    ])
    harness.backend.route("POST", TRANSFER_CONFIRM, lambda _call: {
        "id": TRANSFER_ID, "status": "confirmed", "declared_quantity": 5,
    })

    driver_updates, labels = await sign_in(harness)
    await open_cash_hub(harness, driver_updates, labels)
    await harness.send(driver_updates.tap("staff_bottle_log_loaded"))
    await harness.send(driver_updates.tap("staff_bottle_transfers_pending"))
    await harness.send(driver_updates.tap(f"staff_transfer_custom_{TRANSFER_ID}"))
    assert harness.conversation_state("staff_bottle_transfer_confirm") == BOTTLE_TRANSFER_CONFIRM_QTY_INPUT

    harness.telegram.reset()
    await harness.send(driver_updates.text(menu_label(labels, "staff.menu.cash")))

    assert harness.conversation_state("staff_bottle_transfer_confirm") is None
    assert _curated("staff.cash.hub_title") in harness.telegram.last_shown().text

    harness.telegram.reset()
    await harness.send(driver_updates.text("3"))
    assert not backend_calls(harness, "POST", TRANSFER_CONFIRM), (
        "a number typed after walking away confirmed somebody else's bottle transfer"
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
    assert _curated("staff.delivery.bottle_session_opened").format(
        count=40, ref="SES-2026"
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
    driver.backend.route(
        "GET", SESSION_MEMBERSHIP,
        lambda _call: {"owner_name": "Bek Toshev", "current_inventory": 9},
    )
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
