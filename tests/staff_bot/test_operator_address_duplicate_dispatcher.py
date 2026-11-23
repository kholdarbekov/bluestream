"""The operator's possible-duplicate screen, driven through the real staff dispatcher.

Since the duplicate-address guard (spec 2026-11-18), an operator who saves an address within the configured radius
of one the customer already has gets a 409 `ADDRESS_POSSIBLE_DUPLICATE` naming the saved rows. That is the normal
path, not a race: callers describe the same flat twice. The add-address conversation must keep the typed address,
name the saved rows, and offer three ways on:

* inside an order for this customer, "Use" — the order picker's own `staff_op_addr_<id>`, claimed by the
  create-order conversation still parked at its address step;
* "Create anyway", which posts the same body again with `confirm_duplicate: true`;
* "Back", to the confirm summary the operator came from.

Every test starts at `/start`, reads the buttons off what the bot SENT, and asserts the exact bytes that reached the
backend and the exact screen the operator would see.
"""

from __future__ import annotations

import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.operator.create_order import SELECT_ADDRESS, SELECT_PRODUCTS
from staff_bot.handlers.operator.manage_address import (
    ADDRESS_DUPLICATE,
    CONFIRM_ADDRESS,
    ManageAddressHandler,
)
from staff_bot.utils.flow_state import PENDING_FLOW_USER_DATA_KEYS

from tests.integration.address_row_builders import address_row, possible_duplicate_body
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_flow_state_and_escapes import fire_timeout
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    ADDRESSES_ENDPOINT,
    CLIENT_ID,
    CLIENT_PHONE,
    IN_ZONE_PIN,
    NEW_ADDRESS_ID,
    OperatorWorld,
    _curated,
    _translation_table,
    backend_calls,
    build_staff,
    capture_errors,
    last_screen,
    menu_label,
    sign_in,
    texts,
    user_data,
    wire_backend,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ADD_ADDRESS = "staff_add_address"
CREATE_ORDER = "staff_create_order"

# The copy these screens render, on top of the journey file's table.
DUPLICATE_COPY = (
    "staff.operator.address_duplicate_prompt",
    "staff.operator.address_duplicate_use",
    "staff.operator.address_duplicate_create_anyway",
    "staff.flow_timed_out",
)

# The customer's saved home, as the address endpoints serialize it.
UY_ID = 63
UY = address_row(
    UY_ID,
    title="Uy",
    full_address="Chilonzor 9-kvartal, 14-uy",
    district="Chilonzor",
    latitude=41.3112,
    longitude=69.2798,
)

# What the operator types for the "new" address: the same flat, described again.
TYPED_LABEL = "Uy 2"
TYPED_LINE = "Chilonzor 9-kvartal, 14-uy, 2-kirish"
# The body `confirm_address` posts for it (district and notes skipped with "-").
FIRST_BODY = {
    "title": TYPED_LABEL,
    "full_address": TYPED_LINE,
    "district": None,
    "delivery_notes": None,
    "latitude": IN_ZONE_PIN[0],
    "longitude": IN_ZONE_PIN[1],
}


def possible_duplicate(*candidates):
    """The 409 `StaffService.add_client_address` answers (address_guard_service.assert_not_duplicate), in the envelope
    tests/contract/test_bot_address_fakes_match_producer.py holds to the live route: `error_code` and `details`."""
    return staff_backend_failure(
        "You already have an address saved at this spot.",
        status_code=409,
        error_type="CONFLICT",
        **possible_duplicate_body(candidates),
    )


@pytest.fixture
async def operator(monkeypatch):
    table = _translation_table({
        (language, key): _curated(key, language) for key in DUPLICATE_COPY for language in ("en", "uz", "ru")
    })
    harness = await build_staff(monkeypatch, roles=["operator"], language="en", translations=table)
    harness.world = wire_backend(harness, OperatorWorld())
    harness.world.addresses = [dict(UY)]
    harness.errors = capture_errors(harness)
    return harness


async def type_the_address(harness, ops):
    """From the add-address entry tap to the confirm summary; returns that summary screen."""
    assert harness.conversation_state(ADD_ADDRESS) is not None, "fixture: the add-address conversation is open"
    await harness.send(ops.text(TYPED_LABEL))
    await harness.send(ops.text(TYPED_LINE))
    await harness.send(ops.text("-"))
    await harness.send(ops.text("-"))
    assert harness.conversation_state(ADD_ADDRESS) == CONFIRM_ADDRESS
    summary = last_screen(harness)
    assert summary.callback_data() == ["staff_op_confirm_address", "staff_back_to_main"]
    return summary


async def reach_confirm_outside_an_order(harness):
    """Search the caller, open their addresses, add one: the Manage addresses path, no order in progress."""
    ops, labels = await sign_in(harness)
    await harness.send(ops.text(menu_label(labels, "staff.menu.search_client")))
    await harness.send(ops.text(CLIENT_PHONE))
    await harness.send(ops.tap(f"staff_op_addresses_{CLIENT_ID}"))
    await harness.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    summary = await type_the_address(harness, ops)
    return ops, summary


async def reach_confirm_inside_an_order(harness):
    """Create order for the caller, then Add address from the order's address picker."""
    ops, labels = await sign_in(harness)
    await harness.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await harness.send(ops.text(CLIENT_PHONE))
    await harness.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    assert harness.conversation_state(CREATE_ORDER) == SELECT_ADDRESS
    await harness.send(ops.tap(f"staff_op_add_addr_{CLIENT_ID}"))
    summary = await type_the_address(harness, ops)
    return ops, summary


def candidate_lines(*rows) -> str:
    """The prompt, then each saved row: its name and distance, and its street line when it has a title."""
    lines = [_curated("staff.operator.address_duplicate_prompt", "en")]
    for row in rows:
        lines.append(f"📍 {row['display_label']} · ~{row['distance_m']} m")
        if row["title"]:
            lines.append(f"    {row['full_address']}")
    return "\n".join(lines)


async def test_a_409_at_confirm_shows_the_candidates_and_keeps_the_conversation(operator):
    ops, _summary = await reach_confirm_outside_an_order(operator)
    draft = dict(user_data(operator)["new_address"])
    near = UY | {"distance_m": 14}
    operator.world.address_write = possible_duplicate(near)
    operator.telegram.reset()

    await operator.send(ops.tap("staff_op_confirm_address"))

    assert backend_calls(operator, "POST", ADDRESSES_ENDPOINT)[0].data == FIRST_BODY
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE, (
        "the duplicate refusal ended the conversation, so the operator retypes the whole address"
    )
    assert user_data(operator)["new_address"] == draft
    assert user_data(operator)["adding_address_for"] == CLIENT_ID
    assert user_data(operator)["address_duplicate"] == {"candidates": [near]}

    screen = last_screen(operator)
    assert screen.method == "editMessageText"
    assert screen.params.get("parse_mode") == "HTML"
    assert screen.text == candidate_lines(near)
    # Outside an order there is nothing to "use" the saved row for.
    assert screen.callback_data() == ["staff_op_dup_create", "staff_op_dup_back"]
    assert screen.button_labels() == [
        f"➕ {_curated('staff.operator.address_duplicate_create_anyway', 'en')}",
        f"⬅️ {_curated('staff.back', 'en')}",
    ]
    assert _curated("staff.operator.address_saved", "en") not in " ".join(texts(operator))
    assert operator.errors == []


async def test_create_anyway_posts_again_with_the_flag(operator):
    ops, _summary = await reach_confirm_outside_an_order(operator)
    operator.world.address_write = possible_duplicate(UY | {"distance_m": 14})
    await operator.send(ops.tap("staff_op_confirm_address"))
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE

    # The backend takes the second post: the operator said they saw the saved row.
    operator.world.address_write = None
    operator.telegram.reset()
    await operator.send(ops.tap("staff_op_dup_create"))

    writes = backend_calls(operator, "POST", ADDRESSES_ENDPOINT)
    assert len(writes) == 2
    assert writes[0].data == FIRST_BODY
    assert writes[1].data == {**FIRST_BODY, "confirm_duplicate": True}
    assert [row["id"] for row in operator.world.addresses] == [UY_ID, NEW_ADDRESS_ID]

    done = last_screen(operator)
    assert done.text == f"✅ {_curated('staff.operator.address_saved', 'en')}"
    assert done.callback_data() == ["staff_back_to_main"]
    assert operator.conversation_state(ADD_ADDRESS) is None
    for key in ManageAddressHandler.ADD_ADDRESS_FLOW_KEYS:
        assert key not in user_data(operator), f"{key} outlived the saved address"
    assert operator.errors == []


async def test_use_in_the_order_flow_lands_on_that_address_with_no_add_address_state_left(operator):
    ops, _summary = await reach_confirm_inside_an_order(operator)
    operator.world.address_write = possible_duplicate(UY | {"distance_m": 14})
    confirm_tap = await operator.send(ops.tap("staff_op_confirm_address"))
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE

    screen = last_screen(operator)
    assert screen.callback_data() == [f"staff_op_addr_{UY_ID}", "staff_op_dup_create", "staff_op_dup_back"]
    assert screen.button_labels()[0] == f"📍 {_curated('staff.operator.address_duplicate_use', 'en')}: Uy"

    operator.telegram.reset()
    await operator.send(ops.tap(f"staff_op_addr_{UY_ID}"))

    assert operator.conversation_state(CREATE_ORDER) == SELECT_PRODUCTS
    assert user_data(operator)["new_order"]["delivery_address_id"] == UY_ID
    assert _curated("staff.operator.select_products", "en") in last_screen(operator).text
    for key in ManageAddressHandler.ADD_ADDRESS_FLOW_KEYS:
        assert key not in user_data(operator), f"choosing an address left the add-address detour's {key} behind"
    assert len(backend_calls(operator, "POST", ADDRESSES_ENDPOINT)) == 1, "Use must not save a second address"

    # The add-address conversation is still parked on the duplicate screen with its timer running. When it fires,
    # the operator is three screens into the order: the timeout must say nothing and take nothing.
    order_draft = dict(user_data(operator)["new_order"])
    operator.telegram.reset()
    operator.backend.calls.clear()

    verdict = await fire_timeout(operator, ADD_ADDRESS, confirm_tap)

    assert verdict == ConversationHandler.END
    assert operator.telegram.shown == []
    assert operator.backend.calls == []
    assert operator.conversation_state(CREATE_ORDER) == SELECT_PRODUCTS
    assert user_data(operator)["new_order"] == order_draft
    assert operator.errors == []


async def test_back_returns_to_the_confirm_summary(operator):
    ops, summary = await reach_confirm_outside_an_order(operator)
    draft = dict(user_data(operator)["new_address"])
    operator.world.address_write = possible_duplicate(UY | {"distance_m": 14})
    await operator.send(ops.tap("staff_op_confirm_address"))
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE

    operator.telegram.reset()
    operator.backend.calls.clear()
    await operator.send(ops.tap("staff_op_dup_back"))

    assert operator.conversation_state(ADD_ADDRESS) == CONFIRM_ADDRESS
    screen = last_screen(operator)
    assert screen.text == summary.text
    assert screen.params.get("parse_mode") == "HTML"
    assert screen.callback_data() == ["staff_op_confirm_address", "staff_back_to_main"]
    assert user_data(operator)["new_address"] == draft
    assert "address_duplicate" not in user_data(operator)
    assert operator.backend.calls == [], "Back must not write anything"

    # And Confirm from there asks the backend again, with the same body.
    await operator.send(ops.tap("staff_op_confirm_address"))
    assert [call.data for call in backend_calls(operator, "POST", ADDRESSES_ENDPOINT)] == [FIRST_BODY]
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE
    assert operator.errors == []


def test_the_add_address_keys_are_registered_with_the_ssot():
    assert set(ManageAddressHandler.ADD_ADDRESS_FLOW_KEYS) <= set(PENDING_FLOW_USER_DATA_KEYS)


async def test_a_candidate_name_with_markup_is_escaped(operator):
    ops, _summary = await reach_confirm_outside_an_order(operator)
    # Telegram refuses a message whose HTML does not parse; the fake transport does too, from here on.
    operator.telegram.strict_entities = True
    marked = address_row(
        UY_ID, title="<b>Uy</b> & Co", full_address="Chilonzor <9> & 14-uy", latitude=41.3112, longitude=69.2798,
    ) | {"distance_m": 14}
    operator.world.address_write = possible_duplicate(marked)
    operator.telegram.reset()

    await operator.send(ops.tap("staff_op_confirm_address"))

    screen = last_screen(operator)
    assert screen.refused is None, f"Telegram refused the duplicate screen: {screen.refused}"
    assert screen.text == "\n".join([
        _curated("staff.operator.address_duplicate_prompt", "en"),
        "📍 &lt;b&gt;Uy&lt;/b&gt; &amp; Co · ~14 m",
        "    Chilonzor &lt;9&gt; &amp; 14-uy",
    ])
    assert operator.conversation_state(ADD_ADDRESS) == ADDRESS_DUPLICATE
    assert operator.errors == []
