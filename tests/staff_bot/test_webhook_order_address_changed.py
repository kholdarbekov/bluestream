"""POST /internal/order-address-changed: the driver's "Address changed" alert, end to end.

Spec 2026-10-18-admin-order-address-change, D7 (the alert), D8 (the snapshot, never
deleted) and D8b (the open stop screen). The route is reached the way
`staff.notify_order_address_changed` reaches it: a real aiohttp server built by
`StaffWebhookServer.setup()`, a body signed with the webhook secret, the real rate limiter
and the real dedup. The alert leaves through the staff bot's real PTB `Application`
(tests/staff_bot/ptb_harness.py), and the driver's taps go through the real dispatcher.

The driver starts from the delivery journeys' own helpers: sign in, open the route card,
open the stop, walk it to the door. So `current_delivery` and the route card are whatever
the real handlers left there, not a hand-built dict.

Mocked: Telegram (the harness transport) and the staff backend API (the harness backend,
whose `/delivery/active` is the journeys' `FakeDeliveryDesk`). The route card's Redis is
an in-memory fake when a test needs the card's borrow (D8b), and absent otherwise, which is
production's degraded path.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import test_utils

from staff_bot.utils import route_card_state
from staff_bot.utils.delivery_snapshot import find_active_row
from staff_bot.webhook_server import StaffWebhookServer

from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID, staff_backend_failure
from tests.staff_bot.test_route_card_state import _FakeRedis as _CardRedis
from tests.staff_bot.test_staff_delivery_journey_dispatcher import (  # noqa: F401  (autouse fixture)
    ACTIVE,
    _SEED,
    _isolated_route_card_state,
    active_row,
    build_driver,
    copy_for,
    open_the_stop,
    status_endpoint,
    status_writes,
    walk_to_the_door,
)
from tests.staff_bot.test_webhook_delivery_failed import _FakeRedis, _sign, _webhook_secret  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

ROUTE = "/internal/order-address-changed"
# The copy the alert (and R10's refusal) draws that the delivery journeys' table does not serve.
NEW_KEYS = (
    "staff.notification.order_address_changed",
    "staff.route.open_stop",
    "staff.error.api.delivery_address_changed",
)

OLD_ADDRESS = "15 Bunyodkor shoh ko'chasi"  # `active_row`'s address: the stop as the driver opened it
NEW_ADDRESS = "7 Amir Temur"
NEW_URL = "https://yandex.com/maps/?rtext=~41.33,69.32&rtt=auto"

# The body `notify_order_address_changed` posts: `_driver_payload` plus the event id.
NEW = {
    "telegram_id": DEFAULT_DRIVER_TELEGRAM_ID,
    "delivery_id": 501,
    "delivery_address_id": 902,
    "order_info": {"order_number": "BS-1001"},
    "old_address": OLD_ADDRESS,
    "new_address": NEW_ADDRESS,
    "lat": 41.33,
    "lng": 69.32,
    "door": {
        "address": NEW_ADDRESS,
        "district": "Yunusobod",
        "apartment_number": "12",
        "floor_number": "3",
        "landmark": "Opposite the park",
        "delivery_instructions": "Ring twice",
    },
    "event_id": "order_address_changed:t-1",
}


def _moved_row(status: str = "assigned", **overrides) -> dict:
    """The stop's `/delivery/active` row once the admin's change is committed."""
    return active_row(
        501, "BS-1001", status=status, delivery_address_id=902, address=NEW_ADDRESS,
        destination_latitude=41.33, destination_longitude=69.32, **overrides,
    )


def _alert_text(*, new: str = NEW_ADDRESS) -> str:
    return copy_for("staff.notification.order_address_changed", "en").format(
        number="BS-1001", old=OLD_ADDRESS, new=new,
    )


@asynccontextmanager
async def _driver_hook(monkeypatch, *, card_state=False):
    """A driver's bot with stop 501 on the route, and its webhook server serving HTTP.

    `card_state` gives the route card a Redis, so a borrow (and the screen it noted) is
    remembered: without one, `mark_borrowed` and `note_borrowed_screen` are no-ops.
    """
    harness = await build_driver(monkeypatch, extra_keys=NEW_KEYS)
    harness.desk.active = [active_row(501, "BS-1001")]
    harness.desk.serve(501)
    if card_state:
        route_card_state.configure(_CardRedis())
        route_card_state._locks.clear()

    server = StaffWebhookServer()
    dedup = _FakeRedis()

    async def _connect():
        server._redis, server._redis_connected = dedup, True

    monkeypatch.setattr(server, "_init_redis", _connect)
    server.set_application(harness.application)
    await server.setup()
    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:

        async def post(body: dict):
            raw = json.dumps(body).encode("utf-8")
            response = await client.post(
                ROUTE, data=raw,
                headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": _sign(raw)},
            )
            return response.status, await response.json()

        yield harness, post, server


def _snapshot(harness) -> dict:
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["current_delivery"]


# --------------------------------------------------------------------------- #
# The copy
# --------------------------------------------------------------------------- #


def test_the_alert_copy_passes_the_seeds_own_russian_gate():
    """The seed's rule, called rather than restated (it raises on Latin text). `copy_for`
    first: on an uncurated key the gate would judge a humanised placeholder instead."""
    assert copy_for("staff.notification.order_address_changed", "ru")
    assert _SEED._validate_russian_translations({"staff.notification.order_address_changed"}) is None


# --------------------------------------------------------------------------- #
# The alert (D7)
# --------------------------------------------------------------------------- #


async def test_the_alert_names_both_addresses_and_offers_navigate_and_open_stop(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        status, body = await post(NEW)
        [sent] = harness.telegram.of("sendMessage")
        assert (status, body) == (200, {"success": True, "message": "Notification sent"})
        assert sent.params["chat_id"] == DEFAULT_DRIVER_TELEGRAM_ID
        assert sent.text == _alert_text()
        # The one loud message of an address change: the driver is on the road.
        assert sent.params["disable_notification"] is False and sent.params["parse_mode"] == "HTML"
        rows = sent.reply_markup["inline_keyboard"]
        assert len(rows) == 2
        assert rows[0][0]["url"] == NEW_URL
        assert rows[1][0]["callback_data"] == "staff_view_active_501"
        assert harness.handlers_matching(harness.updates().tap("staff_view_active_501")), (
            "the alert's Open stop button lands on no registered handler"
        )


async def test_html_in_an_address_is_escaped(monkeypatch):
    """RF1. Unescaped, Telegram refuses to parse the message, the bot answers 502, and the
    task retries until it drops the alert."""
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        status, _body = await post({**NEW, "new_address": "Tom & Jerry <Ofis>"})
        [sent] = harness.telegram.of("sendMessage")
    assert status == 200
    assert sent.text == _alert_text(new="Tom &amp; Jerry &lt;Ofis&gt;")


async def test_a_replayed_event_is_sent_once(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        first = await post(NEW)
        replay = await post(NEW)
        sends = harness.telegram.of("sendMessage")
    assert first == (200, {"success": True, "message": "Notification sent"})
    assert replay == (200, {"success": True, "message": "Already processed"})
    assert len(sends) == 1


async def test_a_send_failure_answers_502_and_an_unreachable_driver_200(monkeypatch):
    """A failed send gives its dedup slot back, so the task's retry really re-sends. A
    driver who blocked the bot is a 200: no retry can reach them."""
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        harness.telegram.fail("sendMessage", "Bad Gateway", status=502)
        failed = await post(NEW)
        harness.telegram.clear_failures()
        harness.telegram.reset()
        retry = await post(NEW)
        sends = harness.telegram.of("sendMessage")
    assert failed == (502, {"success": False, "message": "Send failed"})
    assert retry == (200, {"success": True, "message": "Notification sent"})
    assert len(sends) == 1

    async with _driver_hook(monkeypatch) as (harness, post, _server):
        harness.telegram.fail("sendMessage", "Forbidden: bot was blocked by the user", status=403)
        blocked = await post(NEW)
    assert blocked == (200, {"success": False, "message": "Recipient unreachable"})


async def test_a_malformed_delivery_id_is_a_400_and_sends_nothing(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        status, body = await post({**NEW, "delivery_id": "five-oh-one"})
        sends = harness.telegram.of("sendMessage")
    assert (status, body["success"]) == (400, False)
    assert sends == []


async def test_the_route_is_rate_limited(monkeypatch):
    async with _driver_hook(monkeypatch) as (_harness, _post, server):
        assert ROUTE in server._rate_limiters


# --------------------------------------------------------------------------- #
# The snapshot (D8): refreshed from the backend, patched when that fails, never deleted
# --------------------------------------------------------------------------- #


async def test_navigate_after_the_alert_uses_the_new_coordinates(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        driver, _detail = await open_the_stop(harness)
        assert _snapshot(harness)["destination_lat"] == 41.2876  # the stop as opened
        # The new place's own context: only a refresh from the fresh row can carry it.
        harness.desk.active = [_moved_row(place_outstanding_cod_total=50000)]
        assert (await post(NEW))[0] == 200
        assert (_snapshot(harness)["address"], _snapshot(harness)["place_outstanding_cod_total"]) == (
            NEW_ADDRESS, 50000)

        await harness.send(driver.tap("staff_navigate_501"))
        navigate = harness.telegram.of("editMessageText")[-1]
        assert navigate.reply_markup["inline_keyboard"][0][0]["url"] == NEW_URL
        assert NEW_ADDRESS in navigate.text


async def test_a_failed_fetch_patches_the_snapshot_mid_door_and_cash_is_still_collected(monkeypatch):
    """The fetch fails while the driver is at the door. Deleting the snapshot would make
    `has_cash_due({})` False, and the Delivered tap would record no cash; so the address
    fields are patched from the payload and the money keys are left as they were."""
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        driver, _detail = await open_the_stop(harness)
        await walk_to_the_door(harness, driver)
        harness.backend.route("GET", ACTIVE, lambda _call: staff_backend_failure("down", 503))
        assert (await post(NEW))[0] == 200

        snap = _snapshot(harness)
        assert (snap["delivery_id"], snap["destination_lat"], snap["delivery_address_id"],
                snap["expected_cash_to_collect"]) == (501, 41.33, 902, 150000)
        assert {key: snap[key] for key in NEW["door"]} == NEW["door"]
        assert (snap["destination_lng"], snap["destination_latitude"], snap["destination_longitude"]) == (
            69.32, 41.33, 69.32)

        # Typed taps carry no address id: the accepted no-id case once R10 lands (T6).
        await harness.send(driver.tap("staff_status_501_delivered"))
        await harness.send(driver.tap("staff_cash_full_501"))
        assert status_writes(harness)[-1][1]["metadata"]["cash_collected"] == 150000.0


async def test_an_alert_for_a_stop_the_driver_has_not_opened_leaves_the_snapshot_alone(monkeypatch):
    """The driver's one `current_delivery` names another stop: it is not this alert's to touch."""
    async with _driver_hook(monkeypatch) as (harness, post, _server):
        await open_the_stop(harness)
        before = dict(_snapshot(harness))
        harness.desk.active = [_moved_row()]
        assert (await post({**NEW, "delivery_id": 777, "event_id": "order_address_changed:t-2"}))[0] == 200
        assert _snapshot(harness) == before


# --------------------------------------------------------------------------- #
# The open stop screen (D8b)
# --------------------------------------------------------------------------- #


async def test_an_open_stop_detail_is_redrawn_with_the_new_address(monkeypatch):
    async with _driver_hook(monkeypatch, card_state=True) as (harness, post, _server):
        harness.desk.active = [active_row(501, "BS-1001", delivery_address_id=901)]
        _driver, detail = await open_the_stop(harness)
        assert detail.method == "editMessageText"
        assert "staff_status_501_picked_up:901" in detail.callback_data()
        harness.desk.active = [_moved_row()]
        harness.telegram.reset()
        assert (await post(NEW))[0] == 200

        [redrawn] = harness.telegram.of("editMessageText")
        assert (redrawn.params["chat_id"], redrawn.params["message_id"]) == (
            detail.params["chat_id"], detail.params["message_id"])
        assert NEW_ADDRESS in redrawn.text and OLD_ADDRESS not in redrawn.text
        # The same stop view, drawn fresh: its status buttons carry the new address (R10).
        assert redrawn.callback_data() == [c.replace(":901", ":902") for c in detail.callback_data()]


async def test_an_open_navigate_screen_is_redrawn_with_the_new_coordinates(monkeypatch):
    async with _driver_hook(monkeypatch, card_state=True) as (harness, post, _server):
        driver, _detail = await open_the_stop(harness)
        await harness.send(driver.tap("staff_navigate_501"))
        navigate = harness.telegram.last_shown()
        assert navigate.reply_markup["inline_keyboard"][0][0]["url"] != NEW_URL
        harness.desk.active = [_moved_row()]
        harness.telegram.reset()
        assert (await post(NEW))[0] == 200

        [redrawn] = harness.telegram.of("editMessageText")
        assert redrawn.params["message_id"] == navigate.params["message_id"]
        assert redrawn.reply_markup["inline_keyboard"][0][0]["url"] == NEW_URL
        assert redrawn.callback_data() == ["staff_view_active_501"]
        assert NEW_ADDRESS in redrawn.text


async def test_a_cash_prompt_at_the_door_is_left_alone(monkeypatch):
    """The control is the redraw test above: the same fresh row, the same borrowed card.
    Only the screen differs, and a prompt at the door is never pulled away. The prompt IS a
    noted screen of this stop (`SCREEN_STATUS`, which an order cancel takes back); the
    address-change redraw still leaves it alone."""
    async with _driver_hook(monkeypatch, card_state=True) as (harness, post, _server):
        driver, _detail = await open_the_stop(harness)
        await walk_to_the_door(harness, driver)
        await harness.send(driver.tap("staff_status_501_delivered"))  # the cash prompt
        noted = (await route_card_state.load(DEFAULT_DRIVER_TELEGRAM_ID))["borrowed_screen"]
        assert (noted["delivery_id"], noted["screen"]) == (501, route_card_state.SCREEN_STATUS)
        harness.desk.active = [_moved_row()]
        harness.telegram.reset()
        assert (await post(NEW))[0] == 200
        assert harness.telegram.of("editMessageText") == [] and len(harness.telegram.of("sendMessage")) == 1
        assert (await route_card_state.load(DEFAULT_DRIVER_TELEGRAM_ID))["borrowed_screen"] == noted


# --------------------------------------------------------------------------- #
# R10 (strict): a tap answers for the address its screen was drawn with
# --------------------------------------------------------------------------- #

ADDRESS_CHANGED = staff_backend_failure(
    "The address of this order changed", status_code=409, error_code="STAFF_DELIVERY_ADDRESS_CHANGED"
)


def _button(screen, prefix: str) -> str:
    """The one button of `screen` whose callback starts with `prefix`: the tap the driver makes."""
    [callback] = [c for c in screen.callback_data() if c.startswith(prefix)]
    return callback


def _refused_as_moved(harness) -> None:
    shown = harness.telegram.last_shown()
    assert (shown.text, shown.callback_data()) == (
        copy_for("staff.error.api.delivery_address_changed"),
        ["staff_active_deliveries"],
    )
    ud = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
    assert "pending_status" not in ud and "pending_delivery_cash_flow" not in ud
    assert ud["current_delivery"]["delivery_address_id"] == 902  # refreshed or patched, never popped


def test_the_refusal_copy_passes_the_seeds_own_russian_gate():
    assert copy_for("staff.error.api.delivery_address_changed", "ru")
    assert _SEED._validate_russian_translations({"staff.error.api.delivery_address_changed"}) is None


@pytest.mark.parametrize("fetch", ["refreshed", "patched"])
async def test_the_alert_mid_delivered_flow_never_upgrades_the_flows_address(monkeypatch, fetch):
    async with _driver_hook(monkeypatch, card_state=True) as (harness, post, _server):
        harness.desk.active = [active_row(501, "BS-1001", status="arrived", delivery_address_id=901)]
        driver, detail = await open_the_stop(harness)
        delivered = _button(detail, "staff_status_501_delivered")
        assert delivered == "staff_status_501_delivered:901"
        await harness.send(driver.tap(delivered))  # the flow's first step: the cash prompt
        ud = harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]
        assert ud["pending_status"] == {"delivery_id": 501, "new_status": "delivered", "flow_address_id": 901}
        if fetch == "refreshed":
            harness.desk.active = [_moved_row("arrived")]
        else:
            harness.backend.route("GET", ACTIVE, lambda _call: staff_backend_failure("down", 503))
        await post(NEW)
        assert ud["current_delivery"]["delivery_address_id"] == 902  # D8 ran before the last tap

        harness.desk.status_outcomes[501] = ADDRESS_CHANGED
        await harness.send(driver.tap("staff_cash_full_501"))  # the prompt D8b left alone

        assert status_writes(harness) == [(status_endpoint(501), {
            "status": "delivered", "metadata": {"cash_collected": 150000.0}, "delivery_address_id": 901})]
        _refused_as_moved(harness)


async def test_an_arrived_button_drawn_before_the_change_is_refused_after_the_snapshot_refresh(monkeypatch):
    async with _driver_hook(monkeypatch) as (harness, post, _server):  # no card state: D8b redraws nothing
        harness.desk.active = [active_row(501, "BS-1001", status="in_transit", delivery_address_id=901)]
        driver, detail = await open_the_stop(harness)
        arrived = _button(detail, "staff_status_501_arrived")
        assert arrived == "staff_status_501_arrived:901"
        harness.desk.active = [_moved_row("in_transit")]
        await post(NEW)
        assert _snapshot(harness)["delivery_address_id"] == 902

        harness.desk.status_outcomes[501] = ADDRESS_CHANGED
        await harness.send(driver.tap(arrived))
        confirm = harness.telegram.last_shown()
        # The id stays in the flow, not on the confirm button.
        assert confirm.callback_data() == ["staff_execute_status_501_arrived", "staff_view_active_501"]
        await harness.send(driver.tap(confirm.callback_data()[0]))

        assert status_writes(harness) == [(status_endpoint(501), {"status": "arrived", "delivery_address_id": 901})]
        _refused_as_moved(harness)


async def test_a_refusal_the_alert_never_preceded_re_reads_the_stop(monkeypatch):
    """The alert was lost (a failed send), so nothing refreshed the snapshot before the tap.
    The refusal itself re-reads the stop, so the next screen names the new address."""
    async with _driver_hook(monkeypatch) as (harness, _post, _server):
        harness.desk.active = [active_row(501, "BS-1001", status="in_transit", delivery_address_id=901)]
        driver, detail = await open_the_stop(harness)
        harness.desk.active = [_moved_row("in_transit")]  # the admin's change; no alert arrives
        harness.desk.status_outcomes[501] = ADDRESS_CHANGED
        await harness.send(driver.tap(_button(detail, "staff_status_501_arrived")))
        assert _snapshot(harness)["delivery_address_id"] == 901

        await harness.send(driver.tap("staff_execute_status_501_arrived"))

        assert status_writes(harness) == [(status_endpoint(501), {"status": "arrived", "delivery_address_id": 901})]
        _refused_as_moved(harness)


@pytest.mark.parametrize("fresh", ["reopened", "redrawn"])
async def test_a_screen_drawn_after_the_change_sends_the_new_address_and_is_accepted(monkeypatch, fresh):
    async with _driver_hook(monkeypatch, card_state=fresh == "redrawn") as (harness, post, _server):
        harness.desk.active = [active_row(501, "BS-1001", status="in_transit", delivery_address_id=901)]
        driver, _old = await open_the_stop(harness)
        harness.desk.active = [_moved_row("in_transit")]
        harness.telegram.reset()
        await post(NEW)
        if fresh == "reopened":  # the alert's Open stop
            await harness.send(driver.tap("staff_view_active_501"))
            screen = harness.telegram.last_shown()
        else:  # D8b redrew the borrowed stop view in place
            [screen] = harness.telegram.of("editMessageText")
        arrived = _button(screen, "staff_status_501_arrived")
        assert arrived == "staff_status_501_arrived:902"

        await harness.send(driver.tap(arrived))
        await harness.send(driver.tap("staff_execute_status_501_arrived"))

        assert status_writes(harness) == [(status_endpoint(501), {"status": "arrived", "delivery_address_id": 902})]
        # The redraw after the confirm is drawn from the refreshed snapshot, so its next rung names 902 too.
        assert "staff_status_501_delivered:902" in harness.telegram.last_shown().callback_data()


# --------------------------------------------------------------------------- #
# One row lookup for every reader of `/delivery/active`
# --------------------------------------------------------------------------- #


def _listed_by_id(row: dict) -> dict:
    """`row` as listed under the row's other id name, `id`."""
    row = dict(row)
    row["id"] = row.pop("delivery_id")
    return row


async def test_every_reader_finds_a_stop_listed_by_its_id_alias(monkeypatch):
    """The route card, the stop detail, the alert's refresh and redraw, and the re-anchor
    after a lost snapshot all read a row's id through `delivery_snapshot.active_row_id`. Had
    one kept its own copy, it would miss this stop on its own: the alert would fall back to
    "patched" and skip the redraw."""
    async with _driver_hook(monkeypatch, card_state=True) as (harness, post, _server):
        harness.desk.active = [_listed_by_id(active_row(501, "BS-1001"))]
        driver, detail = await open_the_stop(harness)  # asserts the card's button names 501
        assert {"staff_status_501_picked_up", "staff_navigate_501"} <= set(detail.callback_data())

        harness.desk.active = [_listed_by_id(_moved_row(place_outstanding_cod_total=50000))]
        harness.telegram.reset()
        assert (await post(NEW))[0] == 200
        assert _snapshot(harness)["place_outstanding_cod_total"] == 50000  # refreshed, not patched
        [redrawn] = harness.telegram.of("editMessageText")
        assert redrawn.params["message_id"] == detail.params["message_id"]
        assert NEW_ADDRESS in redrawn.text

        # The snapshot is lost (a deploy): the next status tap re-anchors from the list.
        del harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]["current_delivery"]
        await walk_to_the_door(harness, driver)
        await harness.send(driver.tap("staff_status_501_delivered"))
        await harness.send(driver.tap("staff_cash_full_501"))
        assert status_writes(harness)[-1][1]["metadata"]["cash_collected"] == 150000.0


def test_a_bare_list_body_is_read_like_an_items_body():
    """The other body shape the readers accept; the delivery desk serves only `items`."""
    row = _listed_by_id(active_row(501, "BS-1001"))
    assert find_active_row([row], 501) is row
    assert find_active_row({"items": [row]}, 501) is row
    assert find_active_row({"items": [row]}, 502) is None
    assert find_active_row(None, 501) is None
