"""The driver's own state after an admin changed the address of their stop.

Spec 2026-10-18-admin-order-address-change, D8 and D8b. Once the "Address changed" alert
is sent (`StaffWebhookServer.order_address_changed_handler`), three best-effort steps run,
in this order:

1. `fetch_active_row`: the stop's fresh `/delivery/active` row, or None;
2. `refresh_current_delivery`: when the driver's `current_delivery` is this stop, it is
   replaced by the fresh row, or patched from the alert payload when the fetch failed.
   It is never deleted: the at-door readers skip re-anchoring, and an empty snapshot
   files a Delivered tap with no cash (`base.py`, `_anchor_current_delivery`);
3. `rerender_borrowed_stop`: when the borrowed card shows this stop's detail or Navigate
   screen, that one screen is redrawn with the new address. Any other borrowed screen,
   such as a cash or bottle prompt at the door, is left alone.
"""
import logging
from typing import Mapping, Optional

from telegram.error import BadRequest

from staff_bot.api_client import api_client
from staff_bot.handlers.delivery import route_card
from staff_bot.handlers.delivery.stop_views import navigate_view, stop_detail_view
from staff_bot.utils import route_card_state
from staff_bot.utils.delivery_snapshot import (
    apply_address_change,
    find_active_row,
    snapshot_from_active_row,
)

logger = logging.getLogger(__name__)

_REDRAWN_SCREENS = frozenset({route_card_state.SCREEN_STOP, route_card_state.SCREEN_NAVIGATE})


async def fetch_active_row(bot_app, telegram_id: int, delivery_id: int) -> Optional[dict]:
    """The stop's `/delivery/active` row, read with the driver's own token, or None.

    Fetched the way `route_card.update_card_for_driver` fetches the card. None on any
    failure (no token manager, no token, a refused call, the stop no longer on the list,
    an exception); the caller then patches instead of refreshing, and redraws nothing.
    It never raises.
    """
    try:
        token_manager = (getattr(bot_app, "bot_data", None) or {}).get("token_manager")
        if token_manager is None:
            return None
        token = await token_manager.get_valid_token(int(telegram_id), api_client)
        if not token:
            return None
        async with api_client as client:
            response = await client.get_active_deliveries(token)
        if not getattr(response, "success", False):
            return None
        return find_active_row(response.data, delivery_id)
    except Exception as exc:  # noqa: BLE001 -- best-effort; None is the documented answer
        logger.warning("address change: /delivery/active fetch failed for %s: %s", telegram_id, exc)
        return None


def refresh_current_delivery(
    bot_app, telegram_id: int, delivery_id: int, *, row: Optional[Mapping], payload: Mapping
) -> str:
    """Bring the driver's `current_delivery` to the new address, when it names this stop.

    Returns what it did: "untouched" (the snapshot names another stop, or there is none),
    "refreshed" (rebuilt from the fresh row) or "patched" (the fetch failed, so only the
    address fields moved, `apply_address_change`). It never deletes the key.
    """
    # `.get`, never `[]`: `user_data` creates an entry for any id it is indexed with.
    ud = bot_app.user_data.get(telegram_id)
    if ((ud or {}).get("current_delivery") or {}).get("delivery_id") != delivery_id:
        return "untouched"
    if row is not None:
        ud["current_delivery"] = snapshot_from_active_row(row, delivery_id)
        return "refreshed"
    apply_address_change(ud["current_delivery"], payload)
    return "patched"


async def rerender_borrowed_stop(
    bot_app, telegram_id: int, delivery_id: int, *, row: Optional[Mapping], language: str
) -> bool:
    """Redraw the borrowed card's stop screen with the new address. True when it was redrawn.

    Only the screen `route_card_state.note_borrowed_screen` recorded, only while the card
    is borrowed today, and only when that screen is this stop's detail or Navigate.
    Nothing is redrawn without the fresh row: a patched snapshot still names the old place.
    A Navigate screen whose fresh row has no coordinates is left alone too. "Not modified"
    counts as redrawn: the screen already shows the new address.

    Holds the driver's card lock from the read to the edit, so a tap that borrows the card
    for another screen meanwhile (`mark_borrowed` forgets the note under the same lock)
    either lands after this redraw or stops it.

    DEADLOCK CHECK: the caller is the webhook handler, which holds no lock, and nothing in
    here enters `render_route_card` or `mark_borrowed`.
    """
    if row is None:
        return False
    async with route_card_state.get_lock(telegram_id):
        state = await route_card_state.load(telegram_id)
        if not route_card.is_current_borrow(state, route_card.local_date_str()):
            return False
        screen = state.get("borrowed_screen") or {}
        if screen.get("delivery_id") != delivery_id or screen.get("screen") not in _REDRAWN_SCREENS:
            return False
        if screen["screen"] == route_card_state.SCREEN_STOP:
            view = stop_detail_view(row, language)
        else:
            view = navigate_view(snapshot_from_active_row(row, delivery_id), language)
        if view is None:
            return False
        text, keyboard = view
        try:
            await bot_app.bot.edit_message_text(
                chat_id=screen["chat_id"], message_id=screen["message_id"],
                text=text, reply_markup=keyboard, parse_mode="HTML",
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        return True
