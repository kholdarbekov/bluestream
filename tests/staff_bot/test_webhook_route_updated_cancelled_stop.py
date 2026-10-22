"""POST /internal/route-updated for a cancelled stop: the driver's screen of that stop gives way.

Owner, 2026-10-22: when an order is cancelled, the pinned route card of the driver who held the
stop is refreshed with no sound and no alert, and if the driver is looking at that very stop, its
screen gives way. The backend's push (`refresh_driver_route_card_task`) is the silent route-updated
payload plus the cancelled stop's `delivery_id`.

These drive it end to end, the way the address-change alert's tests do
(tests/staff_bot/test_webhook_order_address_changed.py): a real aiohttp server built by
`StaffWebhookServer.setup()`, a body signed with the webhook secret, and the staff bot's real PTB
`Application` (tests/staff_bot/ptb_harness.py). The driver gets to the door through the real
dispatcher, tapping on the card message itself as a driver does, so the borrowed card, its noted
screen, `current_delivery`, the pending status and the armed cash flow are whatever the real
handlers left there.

Mocked: Telegram (the harness transport) and the staff backend API (the journeys'
`FakeDeliveryDesk`). The route card's and the flow mirror's Redis are in-memory fakes.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import test_utils

from staff_bot.handlers.delivery import route_card
from staff_bot.utils import flow_state, route_card_state
from staff_bot.webhook_server import StaffWebhookServer

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_CHAT_ID, DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_route_card_state import _FakeRedis as _CardRedis
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (  # noqa: F401  (autouse fixture)
    _isolated_route_card_state,
    active_row,
    build_driver,
    copy_for,
    menu_label,
    sign_in,
    status_endpoint,
    status_writes,
)
from tests.staff_bot.test_webhook_delivery_failed import _FakeRedis, _sign, _webhook_secret  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROUTE = "/internal/route-updated"
DRIVER = DEFAULT_DRIVER_TELEGRAM_ID
CANCELLED, KEPT = 501, 502


def _cancel_push(delivery_id: int = CANCELLED) -> dict:
    """The body `refresh_driver_route_card_task` posts, key for key."""
    return {
        "driver_id": 55, "telegram_id": DRIVER, "timestamp": "2026-10-22T03:00:00+00:00",
        "event_id": "route_updated:card-refresh-1", "sound": False, "trigger": "order_cancelled",
        "delivery_id": delivery_id,
    }


class _FlowRedis(_CardRedis):
    """The flow mirror's calls: set/get/delete, plus the pipeline the deferred-offer drain uses."""

    def pipeline(self, transaction=True):
        store = self.store

        class _Pipe:
            def __init__(self):
                self.ops = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def lrange(self, key, start, end):
                self.ops.append(lambda: list(store.get(key) or []))

            def lpush(self, key, value):
                self.ops.append(lambda: store.setdefault(key, []).insert(0, value))

            def ltrim(self, key, start, end):
                self.ops.append(lambda: None)

            def expire(self, key, seconds):
                self.ops.append(lambda: None)

            def delete(self, key):
                self.ops.append(lambda: store.pop(key, None) and 1)

            async def execute(self):
                return [op() for op in self.ops]

        return _Pipe()


@asynccontextmanager
async def _driver_hook(monkeypatch, *, cancelled_row=None):
    """A driver's bot with stops 501 and 502 on the route, card and flow state remembered, and
    its webhook server serving HTTP. `cancelled_row` replaces 501's `/delivery/active` row."""
    harness = await build_driver(monkeypatch)
    harness.desk.active = [cancelled_row or active_row(CANCELLED, "BS-1001"), active_row(KEPT, "BS-1002")]
    harness.desk.serve(CANCELLED, KEPT)
    route_card_state.configure(_CardRedis())
    route_card_state._locks.clear()
    flow_state.configure(_FlowRedis())

    server = StaffWebhookServer()
    dedup = _FakeRedis()

    async def _connect():
        server._redis, server._redis_connected = dedup, True

    monkeypatch.setattr(server, "_init_redis", _connect)
    server.set_application(harness.application)
    await server.setup()
    try:
        async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:

            async def post(body: dict):
                raw = json.dumps(body).encode("utf-8")
                response = await client.post(
                    ROUTE, data=raw,
                    headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": _sign(raw)},
                )
                return response.status, await response.json()

            yield harness, post
    finally:
        flow_state.configure(None)


async def _at_the_door(harness, delivery_id: int):
    """Sign in, open the card, open the stop ON the card, walk it to the door and tap Delivered,
    then Partial: the cash prompt arms the flow. Every tap is on the card message, as a driver's
    is. Returns (update factory, the card's message id)."""
    driver, labels = await sign_in(harness)
    await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    card_id = (await route_card_state.load(DRIVER))["message_id"]
    await harness.send(driver.tap(f"staff_view_active_{delivery_id}", message_id=card_id))
    for status in ("picked_up", "in_transit", "arrived"):
        await harness.send(driver.tap(f"staff_status_{delivery_id}_{status}", message_id=card_id))
        await harness.send(driver.tap(f"staff_execute_status_{delivery_id}_{status}", message_id=card_id))
    await harness.send(driver.tap(f"staff_status_{delivery_id}_delivered", message_id=card_id))
    await harness.send(driver.tap(f"staff_cash_partial_{delivery_id}", message_id=card_id))
    return driver, card_id


def _user_data(harness) -> dict:
    return harness.application.user_data[DRIVER]


async def test_the_cash_prompt_of_the_cancelled_stop_gives_way_and_its_flow_is_disarmed(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post):
        driver, card_id = await _at_the_door(harness, CANCELLED)
        # The status tap noted its screen, and the partial tap armed the flow and its mirror.
        card = await route_card_state.load(DRIVER)
        assert card["view"] == route_card_state.VIEW_BORROWED
        assert card["borrowed_screen"] == {
            "delivery_id": CANCELLED, "screen": route_card_state.SCREEN_STATUS,
            "chat_id": DEFAULT_DRIVER_CHAT_ID, "message_id": card_id,
        }
        held = _user_data(harness)
        assert held["current_delivery"]["delivery_id"] == CANCELLED
        assert held["pending_status"]["delivery_id"] == CANCELLED
        assert held["pending_delivery_cash_flow"] == {"delivery_id": CANCELLED, "flow_type": "partial"}
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"
        writes_before = status_writes(harness)

        harness.desk.active = [active_row(KEPT, "BS-1002")]  # the cancel took 501 off the list
        harness.telegram.reset()
        assert await post(_cancel_push()) == (200, {"success": True, "message": "Route-updated handled"})

        # The card is redrawn in place over the prompt: one edit, nothing sent, nothing pinned.
        [redrawn] = harness.telegram.of("editMessageText")
        assert (redrawn.params["chat_id"], redrawn.params["message_id"]) == (DEFAULT_DRIVER_CHAT_ID, card_id)
        _expected_text, expected_keyboard = route_card.build_view(
            harness.desk._serve_active(None), "en", route_card_state.VIEW_NEXT
        )
        assert redrawn.callback_data() == [
            button.callback_data for row in expected_keyboard.inline_keyboard for button in row
            if button.callback_data
        ]
        assert "staff_view_active_502" in redrawn.callback_data()
        assert harness.telegram.of("sendMessage", "pinChatMessage", "editMessageReplyMarkup") == []
        card = await route_card_state.load(DRIVER)
        assert card["view"] == route_card_state.VIEW_NEXT and "borrowed_screen" not in card
        # The stop is left as the backend's cancelled refusal would leave it.
        held = _user_data(harness)
        assert not {"current_delivery", "pending_status", "pending_delivery_cash_flow"} & set(held)
        assert await flow_state.get_active_flow(DRIVER) is None

        # What the driver types next is not money for the cancelled order: neither the amount
        # nor the note reaches the flow, and nothing asks for the partial-cash reason.
        harness.telegram.reset()
        await harness.send(driver.text("120000"))
        await harness.send(driver.text("Customer 30 000 short"))
        assert status_writes(harness) == writes_before
        reason_prompt = copy_for("staff.delivery.enter_partial_cash_reason")
        assert not any(reason_prompt in call.text for call in harness.telegram.shown)


async def test_a_cash_prompt_of_another_stop_keeps_its_screen_its_flow_and_its_snapshot(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post):
        await _at_the_door(harness, KEPT)
        card_before = await route_card_state.load(DRIVER)
        held_before = json.loads(json.dumps(_user_data(harness), default=str))

        harness.desk.active = [active_row(KEPT, "BS-1002", status="arrived")]
        harness.telegram.reset()
        assert (await post(_cancel_push(CANCELLED)))[0] == 200

        assert harness.telegram.calls == []
        assert await route_card_state.load(DRIVER) == card_before
        assert json.loads(json.dumps(_user_data(harness), default=str)) == held_before
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"


async def test_a_borrow_noted_before_this_change_is_left_alone_until_the_backend_refuses_the_tap(monkeypatch):
    """A borrow left by a bot that did not note status screens names no stop: the push leaves
    it, and everything the driver holds, exactly as it was. The driver's next submit reaches the
    backend, whose cancelled refusal clears the flow and says why."""
    async with _driver_hook(monkeypatch) as (harness, post):
        driver, _card_id = await _at_the_door(harness, CANCELLED)
        legacy = await route_card_state.load(DRIVER)
        legacy.pop("borrowed_screen", None)
        await route_card_state.save(DRIVER, legacy)
        held_before = json.loads(json.dumps(_user_data(harness), default=str))

        harness.desk.active = [active_row(KEPT, "BS-1002")]
        harness.telegram.reset()
        assert (await post(_cancel_push()))[0] == 200

        assert harness.telegram.calls == []
        assert await route_card_state.load(DRIVER) == legacy
        assert json.loads(json.dumps(_user_data(harness), default=str)) == held_before
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"

        harness.desk.status_outcomes[CANCELLED] = staff_backend_failure(
            "Cannot transition from 'cancelled' to 'delivered'. Allowed transitions: []",
            status_code=400, error_code="STAFF_INVALID_STATUS_TRANSITION",
            details={"current_status": "cancelled", "requested_status": "delivered"},
        )
        await harness.send(driver.text("120000"))
        await harness.send(driver.text("Customer 30 000 short"))

        assert status_writes(harness)[-1][0] == status_endpoint(CANCELLED)
        assert "pending_delivery_cash_flow" not in _user_data(harness)
        assert "pending_status" not in _user_data(harness)
        assert await flow_state.get_active_flow(DRIVER) is None
        assert copy_for("staff.error.api.status_transition_refused_detail").format(
            current_status=f"❓ {copy_for('staff.delivery.status.cancelled')}",
            requested_status=f"✅ {copy_for('staff.delivery.status.delivered')}",
        ) in harness.telegram.last_shown().text


async def test_a_bottle_prompt_sent_as_a_new_message_loses_its_buttons_when_the_stop_is_cancelled(monkeypatch):
    """The at-door flow's later prompts are new messages: the note prompt after the typed amount,
    then the bottle prompt after the note. Each re-notes the screen, so the cancel push takes the
    latest one back: the card is redrawn in place, and the bottle prompt keeps its text while its
    live buttons give way to the one Open-route-card button. Nothing is sent."""
    bottles = active_row(
        CANCELLED, "BS-1001",
        expected_returnable_bottles=2, customer_bottle_balance=2, place_bottle_balance_signed=2,
    )
    async with _driver_hook(monkeypatch, cancelled_row=bottles) as (harness, post):
        driver, card_id = await _at_the_door(harness, CANCELLED)
        # The id Telegram gives each sent message, keyed by the call that sent it.
        sent_ids = {}
        answer = harness.telegram._message

        def _message(params):
            result = answer(params)
            sent_ids[id(params)] = result["message_id"]
            return result

        monkeypatch.setattr(harness.telegram, "_message", _message)
        await harness.send(driver.text("120000"))
        note_prompt = harness.telegram.of("sendMessage")[-1]
        assert note_prompt.text == copy_for("staff.delivery.enter_partial_cash_reason")
        await harness.send(driver.text("Customer 30 000 short"))
        bottle_prompt = harness.telegram.of("sendMessage")[-1]
        assert any(data.startswith("staff_bottles_") for data in bottle_prompt.callback_data()), (
            "the note did not lead to the bottle prompt"
        )
        noted = (await route_card_state.load(DRIVER))["borrowed_screen"]
        assert noted == {
            "delivery_id": CANCELLED, "screen": route_card_state.SCREEN_STATUS,
            "chat_id": DEFAULT_DRIVER_CHAT_ID, "message_id": sent_ids[id(bottle_prompt.params)],
        }, "the bottle prompt, a new message, is the screen noted"
        assert noted["message_id"] not in {card_id, sent_ids[id(note_prompt.params)]}
        writes_before = status_writes(harness)

        harness.desk.active = [active_row(KEPT, "BS-1002")]
        harness.telegram.reset()
        assert (await post(_cancel_push()))[0] == 200

        [redrawn] = harness.telegram.of("editMessageText")
        assert (redrawn.params["chat_id"], redrawn.params["message_id"]) == (DEFAULT_DRIVER_CHAT_ID, card_id)
        [stripped] = harness.telegram.of("editMessageReplyMarkup")
        assert (stripped.params["chat_id"], stripped.params["message_id"]) == (
            DEFAULT_DRIVER_CHAT_ID, noted["message_id"],
        )
        assert stripped.callback_data() == ["staff_route_view_next"]
        assert harness.telegram.of("sendMessage", "pinChatMessage") == []
        held = _user_data(harness)
        assert not {"current_delivery", "pending_status", "pending_delivery_cash_flow"} & set(held)
        assert await flow_state.get_active_flow(DRIVER) is None
        assert status_writes(harness) == writes_before


async def test_an_old_button_of_the_cancelled_stop_is_refused_after_the_card_was_refreshed(monkeypatch):
    """The driver opened the stop and went back to the card, so the card was not borrowed when
    the cancel arrived, and the snapshot still named the stop. The push still forgets it: an old
    Delivered button of that stop re-reads the active list and is refused as a stale card,
    instead of drawing a cash prompt for a cancelled order."""
    async with _driver_hook(monkeypatch) as (harness, post):
        driver, labels = await sign_in(harness)
        await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
        card_id = (await route_card_state.load(DRIVER))["message_id"]
        await harness.send(driver.tap(f"staff_view_active_{CANCELLED}", message_id=card_id))
        await harness.send(driver.tap("staff_active_deliveries", message_id=card_id))
        assert (await route_card_state.load(DRIVER))["view"] == route_card_state.VIEW_NEXT
        assert _user_data(harness)["current_delivery"]["delivery_id"] == CANCELLED

        harness.desk.active = [active_row(KEPT, "BS-1002")]
        assert (await post(_cancel_push()))[0] == 200
        assert "current_delivery" not in _user_data(harness)

        harness.telegram.reset()
        await harness.send(driver.tap(f"staff_status_{CANCELLED}_delivered", message_id=card_id))

        refusal = harness.telegram.last_shown()
        assert (refusal.text, refusal.callback_data()) == (
            copy_for("staff.delivery.not_found"), ["staff_active_deliveries"],
        )
        assert status_writes(harness) == []


async def test_another_conversation_keeps_its_draft_when_the_stop_is_cancelled(monkeypatch):
    """A push is not a driver gesture. The driver is at the cancelled stop's cash prompt and has
    also opened "Different count" on a bottle transfer. The cancel clears the stop's own at-door
    flow only: the transfer draft survives, and the count the driver types next still confirms
    the transfer. The active-flow mirror goes: the transfer never set it (only the flows in
    `flow_state.ACTIVE_FLOW_MARKER_KEYS` do), so nothing is left to hold pool offers back for."""
    async with _driver_hook(monkeypatch) as (harness, post):
        driver, _card_id = await _at_the_door(harness, CANCELLED)
        confirmed = []
        harness.backend.route(
            "POST", "/api/v1/staff/bottles/transfers/77/confirm",
            lambda call: confirmed.append(call.data) or {"status": "confirmed", "declared_quantity": 5},
        )
        await harness.send(driver.tap("staff_transfer_custom_77", message_id=9_001))
        assert _user_data(harness)["pending_confirm_transfer_id"] == 77
        # The marker the push is meant to drop is really there, and an offer is held back for it.
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"
        offer = {"delivery_id": 777, "order_no": "BS-7777"}
        assert await flow_state.queue_pool_suggestion(DRIVER, offer)

        harness.desk.active = [active_row(KEPT, "BS-1002")]
        harness.telegram.reset()
        assert (await post(_cancel_push()))[0] == 200

        held = _user_data(harness)
        assert held["pending_confirm_transfer_id"] == 77
        assert not {"current_delivery", "pending_status", "pending_delivery_cash_flow"} & set(held)
        assert await flow_state.get_active_flow(DRIVER) is None
        # Drained: the held-back offer went out once, silently, to the driver's chat.
        assert await flow_state.drain_pool_suggestions(DRIVER) == []
        [delivered] = harness.telegram.of("sendMessage")
        assert delivered.params["disable_notification"] is True
        assert delivered.params["chat_id"] == DRIVER
        assert "staff_decline_suggestion_777" in delivered.callback_data()

        await harness.send(driver.text("5"))
        assert confirmed == [{"confirmed_quantity": 5}]


async def test_a_collection_that_owns_the_flow_mirror_keeps_it_when_the_stop_is_cancelled(monkeypatch):
    """The driver started a COD collection, whose typed note step arms the active-flow mirror,
    then walked the cancelled stop to the door and tapped Partial, which re-arms the same mirror.
    The cancel clears the stop's own flow, but the collection is still waiting for typed input,
    so the mirror stays and the pool offer it held back is not delivered."""
    async with _driver_hook(monkeypatch) as (harness, post):
        harness.backend.route(
            "GET", "/api/v1/staff/customers/42/cod-statement",
            lambda _call: {"customer_id": 42, "first_name": "Kamola", "cluster_delivered_outstanding_amount": 50000},
        )
        driver, labels = await sign_in(harness)
        await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
        card_id = (await route_card_state.load(DRIVER))["message_id"]
        await harness.send(driver.tap("staff_cod_collect_full_42", message_id=9_002))
        assert await flow_state.get_active_flow(DRIVER) == "pending_cod_collection_flow"
        cod_flow = dict(_user_data(harness)["pending_cod_collection_flow"])
        await harness.send(driver.tap(f"staff_view_active_{CANCELLED}", message_id=card_id))
        for status in ("picked_up", "in_transit", "arrived"):
            await harness.send(driver.tap(f"staff_status_{CANCELLED}_{status}", message_id=card_id))
            await harness.send(driver.tap(f"staff_execute_status_{CANCELLED}_{status}", message_id=card_id))
        await harness.send(driver.tap(f"staff_status_{CANCELLED}_delivered", message_id=card_id))
        await harness.send(driver.tap(f"staff_cash_partial_{CANCELLED}", message_id=card_id))
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"
        assert _user_data(harness)["pending_cod_collection_flow"] == cod_flow
        offer = {"delivery_id": 777, "order_number": "BS-7777"}
        assert await flow_state.queue_pool_suggestion(DRIVER, offer)

        harness.desk.active = [active_row(KEPT, "BS-1002")]
        harness.telegram.reset()
        assert (await post(_cancel_push()))[0] == 200

        held = _user_data(harness)
        assert not {"current_delivery", "pending_status", "pending_delivery_cash_flow"} & set(held)
        assert held["pending_cod_collection_flow"] == cod_flow
        assert await flow_state.get_active_flow(DRIVER) == "pending_delivery_cash_flow"
        assert harness.telegram.of("sendMessage") == [], "the held-back pool offer was delivered"
        assert await flow_state.drain_pool_suggestions(DRIVER) == [offer]


def test_the_flows_that_arm_the_mirror_are_exactly_the_marker_keys():
    """`ACTIVE_FLOW_MARKER_KEYS` is what the cancel push reads to decide whether the active-flow
    mirror is still owned. Every `mark_active(telegram_id, flow_name)` call in the staff bot must
    name its flow by a literal string, so the set is checked against the source rather than
    restated. A call whose flow name cannot be read fails here, with its file and line."""
    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parents[2] / "staff_bot"
    armed, unreadable = set(), []
    for path in sorted(source_root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) != "mark_active":
                continue
            keyword = next((kw.value for kw in node.keywords if kw.arg == "flow_name"), None)
            name = keyword if keyword is not None else (node.args[1] if len(node.args) > 1 else None)
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                armed.add(name.value)
            else:
                unreadable.append(f"{path}:{node.lineno}")
    assert not unreadable, f"mark_active calls whose flow name is not a string literal: {unreadable}"
    assert armed, "no mark_active call found: the scan is looking in the wrong place"
    assert armed == set(flow_state.ACTIVE_FLOW_MARKER_KEYS)
    assert set(flow_state.ACTIVE_FLOW_MARKER_KEYS) <= set(flow_state.PENDING_FLOW_USER_DATA_KEYS)
