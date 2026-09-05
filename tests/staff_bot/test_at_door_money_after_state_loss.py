"""The at-door money screens, driven after the driver's bot state is gone.

WHY THIS FILE EXISTS
--------------------
``test_staff_delivery_journey_dispatcher.py`` walks a clean, uninterrupted stop.
Every money bug below needs the same thing that file never does: a driver whose
``context.user_data`` has lost a piece between the screen being drawn and the
button being tapped. Telegram keeps the card tappable forever; the bot keeps its
state for one process, and clears parts of it on ordinary navigation.

There are two distinct ways to arrive, and they are NOT the same bug:

1. **A deploy, then a tap.** ``@require_auth`` reads ``authenticated`` out of
   ``user_data``, so a bare post-restart callback is refused with "session
   expired" — the money handler is never reached. What DOES reach it is the real
   two-step sequence: the driver taps a reply-keyboard menu button first, which
   is the one path wired to ``StaffBot._recover_session`` (staff_bot/bot.py),
   and THEN taps the delivery card that has been sitting in the chat. Auth is
   restored; ``current_delivery`` is not. ``_anchor_current_delivery`` used to
   hand back ``{}`` for that, and ``get_cod_cash_projection({})`` is ``0.0`` —
   so a button labelled "✅ Cash collected: 150 000" filed a 0.00 collection,
   and the "Delivered" confirm skipped the cash and bottle steps outright. The
   order closes DELIVERED and still owing, and
   ``DELIVERY_STATUS_TRANSITIONS['delivered'] == []`` means there is no redo at
   the door.

2. **A menu tap, no deploy at all.** ``pending_delivery_cash_flow`` is listed in
   ``flow_state.PENDING_FLOW_USER_DATA_KEYS``, so ANY navigation — a menu
   button, ``/start``, a conversation escape — clears it while ``authenticated``
   and ``current_delivery`` both survive. A driver who confirms the cash, glances
   at another screen, and comes back to the still-live bottle prompt had their
   confirmed cash re-read as ``flow.get('cash_amount', 0)`` → ``0``.

The rule these assert is the one this module already states for the
reconciliation handoff (``submit_reconciliation_all``): *"An empty payload is
not 'no amount' — it is 'server, decide the amount'... Never post an amount-less
handoff again."* It was written for one surface and not applied at the door.
"""

from __future__ import annotations

import pytest

from staff_bot.utils.formatters import format_currency

from tests.staff_bot.test_staff_delivery_journey_dispatcher import (  # noqa: F401
    active_row,
    build_driver,
    copy_for,
    menu_label,
    open_the_stop,
    sign_in,
    status_endpoint,
    status_writes,
    texts,
    walk_to_the_door,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


CASH_DUE = 150000


@pytest.fixture
async def stop_bot(monkeypatch):
    harness = await build_driver(monkeypatch)
    harness.desk.active = [active_row(501, "BS-1001")]
    harness.desk.serve(501)
    return harness


@pytest.fixture
async def bottle_bot(monkeypatch):
    """The same stop, but the customer has empties to hand back."""
    harness = await build_driver(monkeypatch)
    harness.desk.active = [
        active_row(
            501, "BS-1001",
            expected_returnable_bottles=4,
            customer_bottle_balance=4,
        )
    ]
    harness.desk.serve(501)
    return harness


async def a_deploy_then_a_menu_tap(harness, driver):
    """Everything a restart really does, followed by the one gesture that
    recovers the session.

    Clearing `user_data` wholesale is what losing the process does. The menu tap
    is what the driver does next, and it is the only path that reaches
    `_recover_session` — inline callbacks do not, which is why the money
    handlers are reachable at all here.
    """
    harness.application.user_data[driver.user_id].clear()
    labels = await _menu_labels(harness, driver)
    await harness.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    assert harness.application.user_data[driver.user_id].get("authenticated"), (
        "the menu tap did not recover the session, so this test would be "
        "asserting on the 'session expired' path instead of the money path"
    )
    harness.telegram.reset()


async def _menu_labels(harness, driver):
    before = len(harness.telegram.calls)
    await harness.send(driver.command("start"))
    shown = harness.telegram.calls[before:]
    labels = [c for c in shown if c.button_labels()][-1].button_labels()
    harness.telegram.reset()
    return labels


# ---------------------------------------------------------------------------
# 1. A deploy, then a tap on the card that outlived it
# ---------------------------------------------------------------------------


async def test_the_cash_button_records_the_amount_it_names(stop_bot):
    """The button's LABEL carries the figure ("✅ Cash collected: 150 000") and
    its callback carries only the delivery id, so the amount is re-derived from
    the snapshot. With the snapshot gone that derivation returned 0.0 and the
    delivery closed as paid-nothing."""
    driver, _detail = await open_the_stop(stop_bot)
    await walk_to_the_door(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_status_501_delivered"))
    assert "staff_cash_full_501" in stop_bot.telegram.last_shown().callback_data()

    await a_deploy_then_a_menu_tap(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_cash_full_501"))

    recorded = status_writes(stop_bot)[-1]
    assert recorded[0] == status_endpoint(501)
    assert recorded[1]["metadata"]["cash_collected"] == float(CASH_DUE), (
        "the driver tapped a button that says 150 000 and this is what the "
        f"backend was told: {recorded[1]['metadata']}"
    )


async def test_delivered_still_asks_for_the_cash(stop_bot):
    """`has_cash_due({})` is False, so the cash screen was skipped entirely and
    the driver went straight to a bare 'Confirm status: Delivered'."""
    driver, _detail = await open_the_stop(stop_bot)
    await walk_to_the_door(stop_bot, driver)

    await a_deploy_then_a_menu_tap(stop_bot, driver)
    await stop_bot.send(driver.tap("staff_status_501_delivered"))

    offered = stop_bot.telegram.last_shown().callback_data()
    assert "staff_cash_full_501" in offered, (
        f"the cash step vanished; the driver was offered {offered}"
    )
    assert "staff_execute_status_501_delivered" not in offered, (
        "the driver can close the delivery without the money ever being asked for"
    )


# ---------------------------------------------------------------------------
# 2. No deploy at all — one menu tap clears the cash flow
# ---------------------------------------------------------------------------


async def test_a_bottle_tap_never_files_zero_for_cash_that_was_confirmed(bottle_bot):
    """The driver confirms 150 000, glances at another screen, then comes back
    to the bottle prompt still on their phone and taps "All 4 returned".

    `pending_delivery_cash_flow` is in `PENDING_FLOW_USER_DATA_KEYS`, so that
    glance cleared it. `flow.get('cash_amount', 0)` then submitted 0 — with
    `_submit_delivery_completion` attaching "no cash due after COD" on the way
    out, because it treats a falsy amount as "nothing was owed".
    """
    driver, _detail = await open_the_stop(bottle_bot)
    await walk_to_the_door(bottle_bot, driver)
    await bottle_bot.send(driver.tap("staff_status_501_delivered"))
    await bottle_bot.send(driver.tap("staff_cash_full_501"))
    prompt = bottle_bot.telegram.last_shown().callback_data()
    assert "staff_bottles_full_501" in prompt, (
        f"the bottle prompt did not appear; the driver was offered {prompt}"
    )

    writes_before = len(status_writes(bottle_bot))
    labels = await _menu_labels(bottle_bot, driver)
    await bottle_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    bottle_bot.telegram.reset()

    await bottle_bot.send(driver.tap("staff_bottles_full_501"))

    new_writes = status_writes(bottle_bot)[writes_before:]
    zeroed = [
        body for _endpoint, body in new_writes
        if body.get("metadata", {}).get("cash_collected") in (0, 0.0)
    ]
    assert not zeroed, (
        "the confirmed cash was filed as zero on a delivery that owed "
        f"{CASH_DUE}: {zeroed}"
    )


async def test_none_returned_also_keeps_the_confirmed_cash(bottle_bot):
    """"❌ None returned" sits on the same prompt and took the same
    `flow.get('cash_amount', 0)` path. 0 bottles is what the driver meant; 0
    money is not."""
    driver, _detail = await open_the_stop(bottle_bot)
    await walk_to_the_door(bottle_bot, driver)
    await bottle_bot.send(driver.tap("staff_status_501_delivered"))
    await bottle_bot.send(driver.tap("staff_cash_full_501"))

    writes_before = len(status_writes(bottle_bot))
    labels = await _menu_labels(bottle_bot, driver)
    await bottle_bot.send(driver.text(menu_label(labels, "staff.menu.active_deliveries")))
    bottle_bot.telegram.reset()

    await bottle_bot.send(driver.tap("staff_bottles_none_501"))

    new_writes = status_writes(bottle_bot)[writes_before:]
    zeroed = [
        body for _endpoint, body in new_writes
        if body.get("metadata", {}).get("cash_collected") in (0, 0.0)
    ]
    assert not zeroed, f"the confirmed cash was filed as zero: {zeroed}"


async def test_an_uninterrupted_bottle_return_still_closes_the_delivery(bottle_bot):
    """The guard must fire only when the amount is genuinely unknown. A driver
    who never leaves the flow still closes the stop in one pass."""
    driver, _detail = await open_the_stop(bottle_bot)
    await walk_to_the_door(bottle_bot, driver)
    await bottle_bot.send(driver.tap("staff_status_501_delivered"))
    await bottle_bot.send(driver.tap("staff_cash_full_501"))
    bottle_bot.telegram.reset()

    await bottle_bot.send(driver.tap("staff_bottles_full_501"))

    endpoint, body = status_writes(bottle_bot)[-1]
    assert endpoint == status_endpoint(501)
    assert body["metadata"]["cash_collected"] == float(CASH_DUE)
    assert body["metadata"]["bottles_returned"] == 4
    assert copy_for("staff.delivery.cash_recorded").format(
        amount=format_currency(CASH_DUE, language="en")
    ) in texts(bottle_bot)[-1]
