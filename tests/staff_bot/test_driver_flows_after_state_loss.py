"""Two driver surfaces that keep working on state that is no longer there.

WHY THIS FILE EXISTS
--------------------
1. ``cancel_overpayment_collection`` — the driver taps "No" on the
   overpayment confirmation. With ``pending_cod_collection_flow`` gone (any menu
   tap clears it: the key is in ``flow_state.PENDING_FLOW_USER_DATA_KEYS``) the
   handler writes an EMPTY dict straight back and re-renders the amount prompt
   with no debtor banner. The driver is then typing a cash figure into a flow
   that names no order and no customer — a money entry with no target.

2. ``handle_location_update`` — a driver with Live Location running keeps
   emitting ``edited_message`` location updates for as long as the share lasts.
   After a deploy ``@require_auth`` reads the wiped ``user_data`` and refuses
   every one of them, silently, so the dispatcher's view of where that driver is
   quietly stops updating. Nothing on the driver's phone changes, so nothing
   tells them.
"""

from __future__ import annotations

import pytest

from tests.staff_bot.test_staff_operator_journey_dispatcher import (  # noqa: F401
    build_staff,
    menu_label,
    sign_in,
    texts,
    user_data,
    _curated,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


EXPIRED = _curated("staff.flow_timed_out", "en")
DRIVER_LOCATION = "/api/v1/staff/delivery/location"


@pytest.fixture
async def driver(monkeypatch):
    from tests.staff_bot.test_staff_operator_journey_dispatcher import _translation_table

    return await build_staff(
        monkeypatch,
        roles=["delivery_driver"],
        translations=_translation_table({
            ("en", "staff.flow_timed_out"): _curated("staff.flow_timed_out", "en"),
        }),
    )


def restart(harness):
    for data in harness.application.user_data.values():
        data.clear()
    for group in harness.application.handlers.values():
        for handler in group:
            conversations = getattr(handler, "_conversations", None)
            if conversations is not None:
                conversations.clear()
    harness.telegram.reset()


def posted_locations(harness):
    return [c for c in harness.backend.calls if "location" in c.endpoint]


async def test_declining_an_overpayment_does_not_arm_an_empty_cash_flow(driver):
    """Re-prompting for an amount with no order and no customer behind it
    invites the driver to type money into nothing."""
    ops, _labels = await sign_in(driver)
    driver.application.user_data[ops.user_id].pop("pending_cod_collection_flow", None)
    driver.telegram.reset()

    await driver.send(ops.tap("staff_cod_confirm_overpay_no"))

    assert "pending_cod_collection_flow" not in user_data(driver), (
        "an empty cash-collection flow was armed anyway: "
        f"{user_data(driver).get('pending_cod_collection_flow')!r} — the prompt "
        "it renders invites the driver to type money against no order"
    )


async def test_declining_an_overpayment_says_the_flow_is_gone(driver):
    """Silence here looks identical to a working prompt."""
    ops, _labels = await sign_in(driver)
    driver.application.user_data[ops.user_id].pop("pending_cod_collection_flow", None)
    driver.telegram.reset()

    await driver.send(ops.tap("staff_cod_confirm_overpay_no"))

    assert EXPIRED in texts(driver), (
        f"the driver was not told the flow had gone; they saw {texts(driver)}"
    )


async def test_live_location_keeps_reaching_the_backend_after_a_deploy(driver):
    """A Live Location share outlives the process that authorised it. If the
    ticks stop reaching the backend, dispatch's view of the fleet goes stale
    with nothing on anyone's screen to say so."""
    ops, _labels = await sign_in(driver)
    restart(driver)

    await driver.send(ops.location(41.3111, 69.2797))

    assert posted_locations(driver), (
        "the driver's position was not forwarded after the deploy; the bot "
        f"called {[c.endpoint for c in driver.backend.calls]}"
    )
