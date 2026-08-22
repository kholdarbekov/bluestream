"""Where the stale-card guard LIVES.

Every staff screen is its own Telegram message and the driver has ONE
``context.user_data``, so "act on the delivery whose button was tapped, not on
whichever snapshot happens to be loaded" is a rule of the staff bot, not a rule
of one handler class. ``_anchor_current_delivery`` / ``_refuse_stale_card``
first grew inside ``StatusUpdateHandler`` because that is where the money
handlers were; when Navigate needed the same guard, ``ActiveDeliveryHandler``
INSTANTIATED ``StatusUpdateHandler`` mid-handler and called its private methods
— deliberately, to avoid a second copy, but a private call across a class
boundary is a copy waiting to happen the next time someone finds the reach
awkward.

These tests pin the guard to the shared base instead:

* it is DEFINED on ``BaseHandler`` (so a third caller inherits it rather than
  importing a sibling handler),
* both call sites resolve to that one function object — neither shadows it,
* ``active_delivery`` no longer names ``StatusUpdateHandler`` at all, and
* the guard actually WORKS from a plain ``BaseHandler`` subclass that has
  nothing to do with deliveries.

The behaviour the guard protects — the wrong-customer and wrong-order ratchets
— is driven end to end through the real dispatcher in
``test_staff_delivery_journey_dispatcher.py``; this file only says where it
lives.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.delivery import active_delivery as active_delivery_module
from staff_bot.handlers.delivery.active_delivery import ActiveDeliveryHandler
from staff_bot.handlers.delivery.status_update import StatusUpdateHandler
from staff_bot.i18n import i18n


GUARD_METHODS = ("_anchor_current_delivery", "_refuse_stale_card")


@pytest.mark.unit
@pytest.mark.parametrize("name", GUARD_METHODS)
def test_the_stale_card_guard_is_defined_on_the_shared_base(name):
    assert name in vars(BaseHandler), (
        f"{name} is not on BaseHandler, so a handler that needs it has to reach "
        f"into another handler class or copy it"
    )


@pytest.mark.unit
@pytest.mark.parametrize("handler_cls", [ActiveDeliveryHandler, StatusUpdateHandler])
@pytest.mark.parametrize("name", GUARD_METHODS)
def test_every_call_site_inherits_the_one_guard(handler_cls, name):
    assert name not in vars(handler_cls), (
        f"{handler_cls.__name__} defines its own {name}: two expressions of the "
        f"same rule, and the wrong-customer bug is what disagreement costs"
    )
    assert getattr(handler_cls, name) is getattr(BaseHandler, name)


@pytest.mark.unit
def test_navigate_does_not_reach_into_the_status_handler_for_it():
    src = inspect.getsource(active_delivery_module)
    assert "StatusUpdateHandler" not in src, (
        "the Navigate guard is still borrowed from another handler class"
    )


class _PlainStaffHandler(BaseHandler):
    """A staff handler group that knows nothing about deliveries.

    Standing in for the next handler that needs the guard: it must get it by
    inheriting, with no import of a sibling handler module.
    """


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_guard_works_from_a_plain_base_subclass():
    handler = _PlainStaffHandler()
    snapshot = {"delivery_id": 501, "address": "Chilonzor 5"}
    context = MagicMock()
    context.user_data = {"current_delivery": snapshot}
    update = MagicMock()

    # The tapped stop IS the anchored one — nothing to re-read.
    assert await handler._anchor_current_delivery(update, context, 501) is snapshot

    # A card from before the id was carried has nothing to compare against, and
    # stranding the driver mid-trip is worse than trusting it.
    assert await handler._anchor_current_delivery(update, context, None) is snapshot

    # A MISMATCH may never hand back the loaded snapshot: without a token there
    # is no way to learn which stop was tapped, so the only answer is "refuse".
    handler._get_auth_token = AsyncMock(return_value=None)
    assert await handler._anchor_current_delivery(update, context, 502) is None


@pytest.mark.unit
@pytest.mark.anyio
async def test_the_refusal_is_reachable_from_a_plain_base_subclass():
    handler = _PlainStaffHandler()
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query

    await handler._refuse_stale_card(update, "uz")

    kwargs = query.edit_message_text.call_args.kwargs
    assert query.edit_message_text.call_args.args[0] == i18n.get(
        "staff.delivery.not_found", "uz"
    )
    keyboard = kwargs["reply_markup"]
    assert [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ] == ["staff_active_deliveries"], (
        "the refusal must send the driver back to their own list"
    )
